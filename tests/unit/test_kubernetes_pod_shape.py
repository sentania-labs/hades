"""26's pod shape, field by field, for every role (requirement 2 of C8a).

The point of reading each field separately is that a regression in any one of them
fails on its own line. A Pod Security `restricted` namespace refuses the same fields
from outside Crucible, so these assertions and that admission are two enforcements of
one shape; this tier is the one that runs without a cluster.
"""

from __future__ import annotations

from typing import Any

import pytest

from crucible.adapters.execution import k8sspec
from crucible.adapters.execution.kubernetes import KubernetesConfig
from crucible.ports.execution import (
    IDENTITY_MOUNT,
    OUTPUT_MOUNT,
    PACKAGE_CACHE_MOUNT,
    REPO_MOUNT,
    REPORT_MOUNT,
    VERIFY_MOUNT,
    WORK_MOUNT,
    CleanupPolicy,
)
from tests.unit.kubernetes_fixtures import build, pod_of, spec

# Every object an attempt's roles produce, by the prefix its name carries.
ROLE_PREFIXES = (
    "prepare-",
    "worker-",
    "collect-",
    "verify-bundle-",
    "verifier-",
    "reader-",
    "cleaner-",
)


@pytest.fixture
async def ran() -> Any:
    """One whole attempt against the fake API, so every role's Pod has been rendered."""
    api, _registry, provider = build()
    launch = spec()
    workspace = await provider.prepare(launch)
    handle = await provider.launch(workspace, launch)
    while (await provider.observe(handle)).state.value == "running":
        pass
    await provider.collect(handle, workspace, launch)
    await provider.cleanup(workspace, CleanupPolicy.KEEP_DIFF_ONLY, launch)
    return api, provider, launch


@pytest.mark.parametrize("prefix", ROLE_PREFIXES)
async def test_the_pod_security_context_is_26s_verbatim(ran: Any, prefix: str) -> None:
    api, _provider, _launch = ran
    pod = pod_of(api, prefix)
    assert pod["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 1000,
        "runAsGroup": 1000,
        "fsGroup": 1000,
        "fsGroupChangePolicy": "OnRootMismatch",
        "seccompProfile": {"type": "RuntimeDefault"},
    }


@pytest.mark.parametrize("prefix", ROLE_PREFIXES)
async def test_the_container_security_context_is_26s_verbatim(ran: Any, prefix: str) -> None:
    api, _provider, _launch = ran
    container = pod_of(api, prefix)["containers"][0]
    assert container["securityContext"]["allowPrivilegeEscalation"] is False
    assert container["securityContext"]["readOnlyRootFilesystem"] is True
    assert container["securityContext"]["capabilities"] == {"drop": ["ALL"]}


@pytest.mark.parametrize("prefix", ROLE_PREFIXES)
async def test_no_service_account_token_no_host_namespaces_no_service_links(
    ran: Any, prefix: str
) -> None:
    api, _provider, _launch = ran
    pod = pod_of(api, prefix)
    assert pod["automountServiceAccountToken"] is False
    assert pod["serviceAccountName"] == "hades-worker"
    assert pod["enableServiceLinks"] is False
    assert pod["hostNetwork"] is False
    assert pod["hostPID"] is False
    assert pod["hostIPC"] is False
    assert pod["restartPolicy"] == "Never"


@pytest.mark.parametrize("prefix", ROLE_PREFIXES)
async def test_the_runtime_class_is_not_set(ran: Any, prefix: str) -> None:
    """26: a runtime class is not set in this version; the field is the microVM step."""
    api, _provider, _launch = ran
    assert "runtimeClassName" not in pod_of(api, prefix)


@pytest.mark.parametrize("prefix", ROLE_PREFIXES)
async def test_the_limits_come_from_policy_and_the_grace_period_with_them(
    ran: Any, prefix: str
) -> None:
    api, _provider, _launch = ran
    pod = pod_of(api, prefix)
    resources = pod["containers"][0]["resources"]
    assert resources["limits"]["cpu"] == "2000m"
    assert resources["limits"]["memory"] == str(4 * 1024**3)
    assert resources["limits"]["ephemeral-storage"] == "2Gi"
    # CPU requests default to half the limit (issue 93), so a small cluster schedules a
    # Burstable pod instead of demanding the whole limit up front. Memory still requests
    # equal to its limit by default: a worker promised the policy's memory is not the
    # first thing evicted, which would show up as a `lost` attempt nobody caused (16).
    assert resources["requests"] == {"cpu": "1000m", "memory": str(4 * 1024**3)}
    assert pod["terminationGracePeriodSeconds"] == 30


async def test_the_request_fractions_are_configurable_per_policy() -> None:
    """Issue 93: a policy may set its own request fractions, and the worker's writable
    credential init container (rw-narrow harnesses) must track the same fraction as the
    main container or it silently dominates the pod's effective request."""
    codex_image = "crucible-worker:codex-fake-succeed-2"
    api, registry, provider = build()
    registry.register(codex_image, harness="codex", version="0.153.4")
    api.put_harness_secret("hades-harness-codex", {"auth.json": b"{}"})
    launch = spec(
        harness="codex",
        image=codex_image,
        policy={
            "images": {"allowlist": ["crucible-worker:*"]},
            "network": {"mode": "egress-proxy", "egress_allowlist": ["pypi.org", "github.com"]},
            "resources": {
                "cpus": 4,
                "memory": "8GiB",
                "cpu_request_fraction": 0.25,
                "memory_request_fraction": 0.5,
            },
            "limits": {"grace_seconds": 30},
        },
    )
    workspace = await provider.prepare(launch)
    await provider.launch(workspace, launch)
    pod = pod_of(api, "worker-")
    resources = pod["containers"][0]["resources"]
    assert resources["limits"] == {
        "cpu": "4000m",
        "memory": str(8 * 1024**3),
        "ephemeral-storage": "2Gi",
    }
    assert resources["requests"] == {"cpu": "1000m", "memory": str(4 * 1024**3)}
    init = pod["initContainers"][0]
    assert init["resources"]["requests"] == {"cpu": "1000m", "memory": str(4 * 1024**3)}


async def test_the_writable_credential_init_container_carries_its_own_restricted_context() -> None:
    """62: `rw-narrow` harnesses (Codex) copy their credential through a writable init
    container, which carries its own security context rather than inheriting the main
    container's. A future init container added without one would be rejected by Pod
    Security admission at runtime, not caught here, so this asserts it directly."""
    codex_image = "crucible-worker:codex-fake-succeed-2"
    api, registry, provider = build()
    registry.register(codex_image, harness="codex", version="0.153.4")
    api.put_harness_secret("hades-harness-codex", {"auth.json": b"{}"})
    launch = spec(harness="codex", image=codex_image)
    workspace = await provider.prepare(launch)
    await provider.launch(workspace, launch)
    pod = pod_of(api, "worker-")
    assert pod["initContainers"]
    for init in pod["initContainers"]:
        assert init["securityContext"]["allowPrivilegeEscalation"] is False
        assert init["securityContext"]["readOnlyRootFilesystem"] is True
        assert init["securityContext"]["capabilities"] == {"drop": ["ALL"]}


@pytest.mark.parametrize("prefix", ROLE_PREFIXES)
async def test_tmp_and_home_are_memory_backed_and_size_limited(ran: Any, prefix: str) -> None:
    api, _provider, _launch = ran
    pod = pod_of(api, prefix)
    volumes = {v["name"]: v for v in pod["volumes"]}
    for name in ("tmp", "home"):
        assert volumes[name]["emptyDir"]["medium"] == "Memory"
        assert volumes[name]["emptyDir"]["sizeLimit"] == str(512 * 1024**2)
    mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
    assert mounts["/tmp"]["name"] == "tmp"
    assert mounts["/home/worker"]["name"] == "home"


async def test_the_worker_mount_layout(ran: Any) -> None:
    """26's mount layout, at the paths the identity bundle names (06): the bundle tells
    the worker its checkout is at REPO_MOUNT and its report directory at REPORT_MOUNT."""
    api, _provider, _launch = ran
    pod = pod_of(api, "worker-")
    mounts = {m["mountPath"]: m for m in pod["containers"][0]["volumeMounts"]}
    assert mounts[REPO_MOUNT] == {
        "name": "ws",
        "mountPath": REPO_MOUNT,
        "readOnly": False,
        "subPath": "repo",
    }
    assert mounts[REPORT_MOUNT]["subPath"] == "report"
    assert mounts[REPORT_MOUNT]["readOnly"] is False
    assert mounts[IDENTITY_MOUNT]["readOnly"] is True
    volumes = {v["name"]: v for v in pod["volumes"]}
    assert volumes["ws"]["persistentVolumeClaim"]["claimName"] == "ws-01attempt0000000000000000a"
    assert volumes["identity"]["configMap"]["name"] == "identity-01attempt0000000000000000a"
    # Every bundle file is projected at its own relative path, not at a flattened key.
    paths = {item["path"] for item in volumes["identity"]["configMap"]["items"]}
    assert "IDENTITY.md" in paths and "contract.yaml" in paths


async def test_the_preparer_gets_the_whole_claim_and_the_collector_its_leaves(
    ran: Any,
) -> None:
    api, _provider, _launch = ran

    def mounts(prefix: str) -> dict[str, Any]:
        return {m["mountPath"]: m for m in pod_of(api, prefix)["containers"][0]["volumeMounts"]}

    preparer = mounts("prepare-")
    assert preparer[WORK_MOUNT]["readOnly"] is False and "subPath" not in preparer[WORK_MOUNT]

    collector = mounts("collect-")
    # 08: the report directory read-only, an output directory writable. FDY-0140: the
    # checkout is writable, because what the worker left uncommitted is committed.
    assert collector[REPO_MOUNT]["readOnly"] is False
    assert collector[REPORT_MOUNT]["readOnly"] is True
    assert collector[OUTPUT_MOUNT]["readOnly"] is False

    bundle = mounts("verify-bundle-")
    assert bundle[OUTPUT_MOUNT]["readOnly"] is True

    verifier = mounts("verifier-")
    # 11: the verifier sees its own tree and its own log directory, never the
    # collector's output directory.
    assert verifier[REPO_MOUNT]["subPath"] == "output/tree"
    assert verifier[VERIFY_MOUNT]["subPath"] == "verify"
    assert OUTPUT_MOUNT not in verifier
    # FDY-0140: the package caches are claim leaves, never the memory-backed home, and
    # the verifier's is its own: a cache the worker wrote is never what it runs from.
    worker = mounts("worker-")
    assert worker[PACKAGE_CACHE_MOUNT]["subPath"] == "pkg-cache"
    assert verifier[PACKAGE_CACHE_MOUNT]["subPath"] == "pkg-cache-verifier"
    for pod in (worker, verifier):
        assert pod[PACKAGE_CACHE_MOUNT]["readOnly"] is False
    worker_env = {e["name"]: e.get("value") for e in pod_of(api, "worker-")["containers"][0]["env"]}
    assert worker_env["UV_CACHE_DIR"] == "/crucible/pkg-cache/uv"
    assert worker_env["PIP_CACHE_DIR"] == "/crucible/pkg-cache/pip"
    assert worker_env["npm_config_cache"] == "/crucible/pkg-cache/npm"
    verifier_env = {
        e["name"]: e.get("value") for e in pod_of(api, "verifier-")["containers"][0]["env"]
    }
    assert verifier_env["UV_CACHE_DIR"] == "/crucible/pkg-cache/uv"

    reader = mounts("reader-")
    assert reader[WORK_MOUNT]["readOnly"] is True


async def test_the_job_never_retries_and_carries_its_own_deadline(ran: Any) -> None:
    api, _provider, _launch = ran
    job = next(row["body"] for row in api.created if str(row["name"]).startswith("worker-"))
    assert job["spec"]["backoffLimit"] == 0
    assert job["spec"]["template"]["spec"]["restartPolicy"] == "Never"
    # The contract's timeout plus the drain grace: Crucible drains before the deadline
    # and classifies the exit itself (26).
    assert job["spec"]["activeDeadlineSeconds"] == 630


async def test_every_object_carries_26s_four_labels_and_lives_in_the_workers_namespace(
    ran: Any,
) -> None:
    api, _provider, launch = ran
    attempt_objects = [
        row
        for row in api.created
        if (row["body"].get("metadata") or {}).get("labels", {}).get(k8sspec.LABEL_ATTEMPT)
        # The readiness canary and its policy carry the canary's own id, not an attempt's.
        and row["body"]["metadata"]["labels"].get(k8sspec.LABEL_ROLE) != k8sspec.ROLE_CANARY
    ]
    assert attempt_objects
    for row in attempt_objects:
        labels = row["body"]["metadata"]["labels"]
        assert labels[k8sspec.LABEL_ATTEMPT] == launch.attempt_id
        assert labels[k8sspec.LABEL_TASK] == launch.task_id
        assert labels[k8sspec.LABEL_OWNER] == launch.owner
        assert labels[k8sspec.LABEL_ROLE]
        assert row["body"]["metadata"]["namespace"] == "hades-workers"


async def test_the_image_pull_secret_is_on_every_pod_when_one_is_configured(ran: Any) -> None:
    api, _provider, _launch = ran
    for prefix in ROLE_PREFIXES:
        assert pod_of(api, prefix)["imagePullSecrets"] == [{"name": "ghcr-pull"}]


async def test_the_workspace_claim_is_read_write_once_with_the_configured_class() -> None:
    api, _registry, provider = build()
    launch = spec()
    await provider.prepare(launch)
    claim = next(row["body"] for row in api.created if row["kind"] == "persistentvolumeclaims")
    assert claim["spec"]["accessModes"] == ["ReadWriteOnce"]
    assert claim["spec"]["storageClassName"] == "lab-ssd"
    assert claim["spec"]["resources"]["requests"]["storage"] == "20Gi"


async def test_a_bundle_above_the_configmap_cap_is_refused_rather_than_truncated() -> None:
    """08, 26: a ConfigMap has a size cap and the projected-volume form above it is not
    implemented. A truncated identity bundle is a worker given the wrong contract."""
    _api, _registry, provider = build(config=KubernetesConfig(poll_interval_seconds=0))
    launch = spec()
    launch.contract["objective"] = "x" * (1024 * 1024 + 1)
    with pytest.raises(Exception, match="above the ConfigMap cap"):
        await provider.prepare(launch)


# ----- reading a live Pod back (issues 66, 76) -------------------------------------


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("2", 2.0),
        ("1500m", 1.5),
        ("0.5", 0.5),
        ("4Gi", 4.0 * 1024**3),
        ("512Mi", 512.0 * 1024**2),
        ("1G", 1e9),
        ("1e3", 1000.0),
        ("4294967296", 4294967296.0),
        (3, 3.0),
    ],
)
def test_a_quantity_is_read_by_value(text: Any, value: float) -> None:
    assert k8sspec.quantity(text) == pytest.approx(value)


@pytest.mark.parametrize("text", ["", None, "lots", "-1", "Gi", True])
def test_what_is_not_a_quantity_reads_as_none(text: Any) -> None:
    assert k8sspec.quantity(text) is None


def test_limits_read_back_from_a_rendered_pod_equal_the_ones_it_was_rendered_from() -> None:
    limits = k8sspec.limits_from_policy(
        {
            "resources": {
                "cpus": 1.5,
                "memory": "3GiB",
                "tmpfs_per_mount": "256MiB",
                "cpu_request_fraction": 0.25,
                "memory_request_fraction": 0.5,
            },
            "limits": {"grace_seconds": 45},
        }
    )
    pod = k8sspec.pod_spec(
        k8sspec.PodRequest(
            role=k8sspec.ROLE_WORKER,
            image="crucible-worker:1",
            command=["true"],
            limits=limits,
            volumes=k8sspec.base_volumes(limits),
        )
    )
    read = k8sspec.limits_from_pod(pod, k8sspec.limits_from_policy({}))
    assert read.as_dict() == limits.as_dict()


def test_a_pod_missing_a_field_keeps_the_fallback_for_that_field_only() -> None:
    fallback = k8sspec.limits_from_policy({"limits": {"grace_seconds": 30}})
    read = k8sspec.limits_from_pod(
        {"containers": [{"name": "crucible", "resources": {"limits": {"memory": "1Gi"}}}]},
        fallback,
    )
    assert read.memory_bytes == 1024**3
    assert read.cpus == fallback.cpus
    assert read.grace_seconds == 30
    assert read.tmpfs_bytes == fallback.tmpfs_bytes


def test_a_live_cpu_request_survives_a_missing_limit() -> None:
    """A live Pod can carry a request with no limit, or a limit admission could not
    parse. The request still says something about the attempt's actual usage, so it is
    kept against the fallback limit instead of being discarded for the policy
    default (issue 76 follow-up)."""
    fallback = k8sspec.limits_from_policy({"resources": {"cpus": 2.0}})
    read = k8sspec.limits_from_pod(
        {
            "containers": [
                {
                    "name": "crucible",
                    "resources": {"requests": {"cpu": "500m"}},
                }
            ]
        },
        fallback,
    )
    assert read.cpus == fallback.cpus
    assert read.cpu_request_fraction == pytest.approx(0.5 / 2.0)


def test_a_live_memory_request_survives_an_unparsable_limit() -> None:
    fallback = k8sspec.limits_from_policy({"resources": {"memory": "4GiB"}})
    read = k8sspec.limits_from_pod(
        {
            "containers": [
                {
                    "name": "crucible",
                    "resources": {
                        "requests": {"memory": "1Gi"},
                        "limits": {"memory": "not-a-quantity"},
                    },
                }
            ]
        },
        fallback,
    )
    assert read.memory_bytes == fallback.memory_bytes
    assert read.memory_request_fraction == pytest.approx(1024**3 / fallback.memory_bytes)
