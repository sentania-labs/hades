"""Issue 357: ro credential projection adds the template source only when there
are templates.

In ro mode the credential directory is one projected volume: the credential
Secret's items plus the identity ConfigMap's `harness/*` template items. When a
harness declares no templates, the old code still added the ConfigMap source
with an empty `items` list. Kubernetes treats an empty or absent `items` as
"project every key", so every file of the attempt's identity ConfigMap
(IDENTITY.md and the rest) would appear inside that harness's credential
directory.

The fix adds the ConfigMap source only when there is at least one template
item, and sets the mode per item rather than `defaultMode` on the ConfigMap
source (`ConfigMapProjection` has no such field; the API silently drops it).
"""

from __future__ import annotations

from crucible.adapters.execution.k8sspec import Limits
from crucible.adapters.execution.kubernetes import KubernetesConfig, _CredentialCopy
from crucible.ports.harness import AuthFile, CredentialSpec, MountMode
from tests.unit.kubernetes_fixtures import build, pod_of, spec

LIMITS = Limits(
    cpus=1,
    memory_bytes=2_147_483_648,
    ephemeral_storage="10Gi",
    tmpfs_bytes=536_870_912,
    grace_seconds=30,
)


def _ro_copy(*, templates: dict[str, str]) -> _CredentialCopy:
    cred_spec = CredentialSpec(
        harness="claude_code",
        mount_target="/home/worker/.claude",
        auth_files=(AuthFile(name="oauth-token"),),
        minimum_mode=MountMode.RO,
        required_for_launch=True,
        templates=templates,
    )
    return _CredentialCopy(
        spec=cred_spec,
        source_secret="crucible-harness-claude-code",
        mode=MountMode.RO,
    )


async def test_ro_without_templates_projects_secret_source_only() -> None:
    """AC1: an ro credential with no templates yields a projection with the
    Secret source only; no ConfigMap source is added."""
    _, _, provider = build(
        config=KubernetesConfig(
            poll_interval_seconds=0,
            credential_modes={"claude_code": MountMode.RO},
        ),
    )

    mounts, volumes, _ = provider._credential_mounts(
        spec=spec(harness="claude_code"),
        copy=_ro_copy(templates={}),
        image="crucible-worker:fake",
        limits=LIMITS,
        present=["oauth-token"],
        identity_paths={},
    )

    cred_volume = next(v for v in volumes if v["name"] == "cred")
    sources = cred_volume["projected"]["sources"]

    configmap_sources = [s for s in sources if "configMap" in s]
    assert configmap_sources == []

    secret_sources = [s for s in sources if "secret" in s]
    assert len(secret_sources) == 1
    secret_keys = {item["key"] for item in secret_sources[0]["secret"]["items"]}
    assert secret_keys == {"oauth-token"}

    cred_mount = next(m for m in mounts if m.path == "/home/worker/.claude")
    assert cred_mount.read_only is True


async def test_ro_with_templates_still_projects_settings_json_at_root() -> None:
    """AC1: the ro projection for a harness with templates is unchanged, and the
    ConfigMap source carries no `defaultMode` (dropped by the API); the mode is
    set per item instead."""
    api, registry, provider = build(
        harness="claude_code",
        config=KubernetesConfig(
            poll_interval_seconds=0,
            credential_modes={"claude_code": MountMode.RO},
        ),
    )
    image = "crucible-worker:claude-fake-succeed-2"
    registry.register(image, harness="claude_code", version="2.1.277")
    api.put_harness_secret(
        "crucible-harness-claude-code",
        {"oauth-token": b"not-a-real-value"},
    )
    launch = spec(harness="claude_code", image=image)
    workspace = await provider.prepare(launch)
    await provider.launch(workspace, launch)
    pod = pod_of(api, "worker-")

    cred_volume = next(v for v in pod["volumes"] if v["name"] == "cred")
    sources = cred_volume["projected"]["sources"]

    configmap_sources = [s for s in sources if "configMap" in s]
    assert len(configmap_sources) == 1
    cm_source = configmap_sources[0]["configMap"]
    assert "defaultMode" not in cm_source

    items_by_path = {item["path"]: item for item in cm_source["items"]}
    assert "settings.json" in items_by_path
    assert items_by_path["settings.json"]["mode"] == 0o444
