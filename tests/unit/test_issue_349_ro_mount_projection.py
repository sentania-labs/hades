"""Issue 349: Claude Code workers cannot start on Kubernetes with the credential
mounted ro.

In ro mode the credential directory is the Secret volume itself, read-only.
A subPath file mount from the identity ConfigMap inside that directory fails
(runc cannot create a file mountpoint inside a read-only Secret mount).

The fix builds a projected read-only volume whose sources are the Secret items
plus the identity ConfigMap template items, all at the same mount target.

No mount in ro mode targets a path under another mount's target.
"""

from __future__ import annotations

from typing import Any

import pytest

from crucible.adapters.execution.kubernetes import KubernetesConfig
from crucible.ports.harness import MountMode
from tests.unit.kubernetes_fixtures import build, pod_of, spec


@pytest.fixture
async def ro_claude_code_attempt() -> tuple[Any, ...]:
    """One Claude Code attempt in ro mode, so the worker Pod has been rendered."""
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
        "hades-harness-claude-code",
        {"oauth-token": b"not-a-real-value"},
    )
    launch = spec(harness="claude_code", image=image)
    workspace = await provider.prepare(launch)
    await provider.launch(workspace, launch)
    pod = pod_of(api, "worker-")
    return api, pod, provider


async def test_ro_credential_is_a_projected_volume(ro_claude_code_attempt: tuple[Any, ...]) -> None:
    """AC1: in ro mode the credential directory is one projected read-only volume
    carrying the Secret items and the harness template; no mount targets a path
    under another mount's target."""
    _api, pod, _provider = ro_claude_code_attempt

    cred_volume = next(v for v in pod["volumes"] if v["name"] == "cred")
    assert "projected" in cred_volume

    sources = cred_volume["projected"]["sources"]
    # There should be exactly two sources: Secret + ConfigMap
    secret_sources = [s for s in sources if "secret" in s]
    configmap_sources = [s for s in sources if "configMap" in s]
    assert len(secret_sources) == 1
    assert len(configmap_sources) == 1

    # The Secret only carries the present auth file (oauth-token); the absent
    # optional file (.claude.json) must not be projected.
    secret_keys = {item["key"] for item in secret_sources[0]["secret"]["items"]}
    assert secret_keys == {"oauth-token"}

    # The ConfigMap source carries the template(s) from the identity bundle,
    # projected at the credential root with leaf names (P1 fix).
    cm_items = {item["path"] for item in configmap_sources[0]["configMap"]["items"]}
    assert "settings.json" in cm_items

    # The mount at the credential target is read-only, from the cred volume.
    mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["/home/worker/.claude"] == {
        "mountPath": "/home/worker/.claude",
        "name": "cred",
        "readOnly": True,
    }


async def test_ro_no_mount_targets_path_under_another_mount_target(
    ro_claude_code_attempt: tuple[Any, ...],
) -> None:
    """AC1 follow-up: in ro mode no mount targets a path under another mount's
    target among credential-related mounts.

    In the old code the identity ConfigMap template was subPath-mounted at
    `/home/worker/.claude/settings.json` on top of the Secret mount at
    `/home/worker/.claude`, so one mount target was a child of another.
    The projected volume form mounts everything at one path."""
    _, pod, _ = ro_claude_code_attempt

    # Only check mounts whose names are `cred` or `identity`; the `home`
    # emptyDir at `/home/worker` is a parent that the credential mount
    # naturally falls under, and that is fine (it is a different volume).
    cred_mounts = [
        m for m in pod["containers"][0]["volumeMounts"] if m["name"] in ("cred", "identity")
    ]
    cred_paths = [m["mountPath"] for m in cred_mounts]

    for i, a in enumerate(cred_paths):
        for b in cred_paths[i + 1 :]:
            assert not b.startswith(a + "/"), (
                f"Credential mount {b} is under mount {a}; no mount should "
                "target a path under another mount's target (issue 349)"
            )


async def test_rw_narrow_unchanged() -> None:
    """AC3: rw-narrow behaviour is unchanged; the credential still comes from the
    claim leaf via subPath and the templates are still identity subPath mounts."""
    api, registry, provider = build(
        config=KubernetesConfig(
            poll_interval_seconds=0,
            credential_modes={"codex": MountMode.RW_NARROW},
        ),
    )
    codex_image = "crucible-worker:codex-fake-succeed-2"
    registry.register(codex_image, harness="codex", version="0.153.4")
    api.put_harness_secret("hades-harness-codex", {"auth.json": b"{}"})
    launch = spec(harness="codex", image=codex_image)
    workspace = await provider.prepare(launch)
    await provider.launch(workspace, launch)
    pod = pod_of(api, "worker-")

    mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
    # rw-narrow: the credential comes from the workspace claim subPath, not a
    # projected Secret volume.
    assert mounts["/home/worker/.codex"] == {
        "mountPath": "/home/worker/.codex",
        "name": "ws",
        "readOnly": False,
        "subPath": "credential",
    }
    # The template is still a subPath mount from the identity ConfigMap.
    assert mounts["/home/worker/.codex/config.toml"]["subPath"] == "harness/config.toml"
    # The cred volume is a plain Secret (not projected).
    cred_volume = next(v for v in pod["volumes"] if v["name"] == "cred-source")
    assert "secret" in cred_volume
    assert "projected" not in cred_volume


async def test_ro_without_templates_still_mounts_credential_target() -> None:
    """P2: an ro credential with no templates still gets its mount at target.

    The fixture above uses Claude Code which has templates, so this test
    calls _credential_mounts directly with an empty template set to exercise
    the ro-credential fix: the base ro mount must be present regardless of
    whether any templates are declared."""
    from crucible.adapters.execution.k8sspec import Limits  # noqa: PLC0415
    from crucible.adapters.execution.kubernetes import _CredentialCopy  # noqa: PLC0415
    from crucible.ports.harness import AuthFile, CredentialSpec, MountMode  # noqa: PLC0415
    from tests.unit.kubernetes_fixtures import build, spec  # noqa: PLC0415

    _, _, provider = build(
        config=KubernetesConfig(
            poll_interval_seconds=0,
            credential_modes={"claude_code": MountMode.RO},
        ),
    )

    # A credential copy with ro mode and zero templates.
    cred_spec = CredentialSpec(
        harness="claude_code",
        mount_target="/home/worker/.claude",
        auth_files=(AuthFile(name="oauth-token"),),
        minimum_mode=MountMode.RO,
        required_for_launch=True,
        templates={},  # no templates
    )
    copy = _CredentialCopy(
        spec=cred_spec,
        source_secret="hades-harness-claude-code",
        mode=MountMode.RO,
    )

    mounts, volumes, _ = provider._credential_mounts(
        spec=spec(harness="claude_code"),
        copy=copy,
        image="crucible-worker:fake",
        limits=Limits(
            cpus=1,
            memory_bytes=2_147_483_648,
            ephemeral_storage="10Gi",
            tmpfs_bytes=536_870_912,
            grace_seconds=30,
        ),
        present=["oauth-token"],
        identity_paths={"harness/settings.json": "harness/settings.json"},
    )

    # The mount at the credential target must exist (P2: was missing in the
    # original code when templates were empty).
    cred_mount = next(
        (m for m in mounts if m.path == "/home/worker/.claude"),
        None,
    )
    assert cred_mount is not None
    assert cred_mount.read_only is True

    # The volume is a projection carrying the Secret items.
    cred_vol = next((v for v in volumes if v["name"] == "cred"), None)
    assert cred_vol is not None
    assert "projected" in cred_vol
    src = cred_vol["projected"]["sources"]
    secret_src = [s for s in src if "secret" in s]
    assert len(secret_src) == 1
    secret_keys = {item["key"] for item in secret_src[0]["secret"]["items"]}
    assert secret_keys == {"oauth-token"}
