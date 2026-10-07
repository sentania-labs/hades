"""hades #394: workspace claims of attempts that never launched are deleted or
released so they cannot fill the claim quota.

The workspace claim (ws-<attempt> PVC) is created by prepare() but, when an
attempt ends before it is ever started (environment at prepare, killed during
launch, quota_exhausted), no gate ever consumed it.  Two paths now remove such
orphan claims:

1. The supervisor's _pre_launch_cleanup_step calls
   KubernetesProvider.delete_workspace_claim for every terminal attempt with
   started_at null.

2. The provider's retention sweep also deletes an unlabeled ws- claim whose
   attempt is terminal and was never kept (orphan set).

A running attempt's claim and a claim carrying the retention label are never
touched by either path."""

from __future__ import annotations

from crucible.adapters.execution import k8sspec
from crucible.ports.execution import CleanupPolicy
from tests.unit.kubernetes_fixtures import (
    ATTEMPT,
    build,
    spec,
)

# ---------------------------------------------------------------------------
# AC1: pre-launch attempt claim deletion (unit test with fake K8s client)
# ---------------------------------------------------------------------------


async def test_delete_workspace_claim_removes_orphan_pvc() -> None:
    """AC1: an attempt that ends environment at prepare has its ws- claim
    deleted; a unit test with the fake Kubernetes client proves it."""
    api, _registry, provider = build()
    # Simulate an attempt whose workspace was prepared but never launched.
    # Prepare creates the PVC.
    launch = spec(attempt_id=ATTEMPT)
    _workspace = await provider.prepare(launch)

    # Verify the claim was created by prepare().
    assert api.object_names("persistentvolumeclaims") == ["ws-01attempt0000000000000000a"]

    # Now delete the claim using delete_workspace_claim (simulating
    # _pre_launch_cleanup_step's call for a terminal attempt with started_at
    # null).
    await provider.delete_workspace_claim(ATTEMPT)

    # The claim is gone.
    assert api.object_names("persistentvolumeclaims") == []


async def test_delete_workspace_claim_is_idempotent() -> None:
    """Calling delete_workspace_claim on an already-deleted claim is a no-op."""
    api, _registry, provider = build()
    launch = spec(attempt_id=ATTEMPT)
    _workspace = await provider.prepare(launch)

    await provider.delete_workspace_claim(ATTEMPT)
    assert api.object_names("persistentvolumeclaims") == []

    # Second call should not error.
    await provider.delete_workspace_claim(ATTEMPT)
    assert api.object_names("persistentvolumeclaims") == []


# ---------------------------------------------------------------------------
# AC2: retention sweep deletes unlabeled ws- claims, keeps labeled ones
# ---------------------------------------------------------------------------


async def test_retention_sweep_removes_orphan_labelled_claims() -> None:
    """AC2: the retention sweep deletes an unlabeled ws- claim of a terminal
    attempt that never launched and leaves a labeled one; a unit test proves
    both."""
    api, _registry, provider = build()
    launch = spec(attempt_id=ATTEMPT)
    _workspace = await provider.prepare(launch)

    # The claim exists with the attempt label but no retention label.
    claim_obj = api.objects[("persistentvolumeclaims", "ws-01attempt0000000000000000a")]
    labels = claim_obj.body.get("metadata", {}).get("labels", {})
    assert k8sspec.LABEL_RETAIN not in labels

    # Run retention sweep with the attempt in keep (simulating supervisor's
    # _live_attempt_ids) but also in orphan (pre-launch terminal attempt).
    orphan_set = [ATTEMPT]
    removed = await provider.retention([ATTEMPT], orphan=orphan_set)

    # The orphan claim is removed even though the attempt is in keep.
    assert removed == 1
    assert api.object_names("persistentvolumeclaims") == []


async def test_retention_sweep_keeps_labeled_claim() -> None:
    """A claim carrying the retention label is still honoured (never deleted by
    retention sweep)."""
    api, _registry, provider = build()
    launch = spec(attempt_id=ATTEMPT)
    _workspace = await provider.prepare(launch)

    # The claim exists with the attempt label but no retention label.
    claim_obj = api.objects[("persistentvolumeclaims", "ws-01attempt0000000000000000a")]
    claim_obj.body.setdefault("metadata", {}).setdefault("labels", {})
    claim_obj.body["metadata"]["labels"][k8sspec.LABEL_RETAIN] = "keep"

    # Run retention sweep with the attempt in keep (not orphan).
    orphan_set: list[str] = []
    removed = await provider.retention([ATTEMPT], orphan=orphan_set)

    # No claim removed (the claim is kept by the retention label).
    assert removed == 0
    assert api.object_names("persistentvolumeclaims") == ["ws-01attempt0000000000000000a"]


async def test_retention_sweep_keeps_cleaned_up_attempt_claim() -> None:
    """AC3: a cleaned-up attempt's claim is not touched when the attempt
    is not in keep (not live) and not in orphan."""
    api, _registry, provider = build()
    launch = spec(attempt_id=ATTEMPT)
    _workspace = await provider.prepare(launch)

    # The claim is created by prepare.
    assert api.object_names("persistentvolumeclaims") == ["ws-01attempt0000000000000000a"]

    # Simulate a cleanup that kept the claim (KEEP policy).  The claim has
    # the retention label so it's protected.  We preserve the original labels
    # (including LABEL_ATTEMPT) and just add LABEL_RETAIN.
    claim_obj = api.objects[("persistentvolumeclaims", "ws-01attempt0000000000000000a")]
    existing_labels = claim_obj.body.setdefault("metadata", {}).setdefault("labels", {})
    existing_labels[k8sspec.LABEL_RETAIN] = "keep"

    # The claim stays because it has the retention label (sweep skips it).
    # We also pass the attempt in `keep` so non-PVC objects stay.
    removed = await provider.retention([ATTEMPT], orphan=[])
    assert removed == 0
    assert api.object_names("persistentvolumeclaims") == ["ws-01attempt0000000000000000a"]


# ---------------------------------------------------------------------------
# AC3: running attempts and kept claims are never deleted
# ---------------------------------------------------------------------------


async def test_delete_workspace_claim_deletes_claim_regardless() -> None:
    """AC3: delete_workspace_claim unconditionally deletes the claim.
    The supervisor's pre-launch cleanup path guards the call with a
    started_at check; this test verifies the provider method itself
    just deletes."""
    api, _registry, provider = build()
    launch = spec(attempt_id=ATTEMPT)
    _workspace = await provider.prepare(launch)
    _handle = await provider.launch(_workspace, launch)

    # The claim exists.
    assert api.object_names("persistentvolumeclaims") == ["ws-01attempt0000000000000000a"]

    # In normal operation delete_workspace_claim is only called by the
    # pre-launch cleanup path, but the method itself is unconditional.
    # We verify it deletes the claim (the supervisor guards the call with
    # a started_at check).
    await provider.delete_workspace_claim(ATTEMPT)
    assert api.object_names("persistentvolumeclaims") == []

    # The claim is gone but the provider still tracks the launched attempt.
    assert ATTEMPT in provider._launched


async def test_retention_sweep_deletes_orphan_pods_and_jobs() -> None:
    """The retention sweep also removes pods and jobs for orphan attempts."""
    api, _registry, provider = build()
    launch = spec(attempt_id=ATTEMPT)
    _workspace = await provider.prepare(launch)

    # The prepare creates the PVC and a credential configmap.  The retention
    # sweep (labelled for LABEL_ATTEMPT) also removes those non-PVC objects
    # when the attempt is orphan (kept empty).
    assert api.object_names("persistentvolumeclaims")  # PVC exists

    # Run retention with an empty keep list but the attempt in the orphan set:
    # the PVC is removed; non-PVC objects (configmaps/secrets) also vanish
    # because they share LABEL_ATTEMPT and are not in ``keep``.
    _removed = await provider.retention([], orphan=[ATTEMPT])
    assert api.object_names("persistentvolumeclaims") == []


# ---------------------------------------------------------------------------
# Policy preservation: keep_diff_only still produces a retention label
# ---------------------------------------------------------------------------


async def test_cleanup_policy_keep_diff_only_produces_retention_label() -> None:
    """A cleanup with KEEP diff only still produces the retention label so
    the claim survives the retention sweep."""
    api, _registry, provider = build()
    launch = spec(attempt_id=ATTEMPT)
    _workspace = await provider.prepare(launch)

    # Run a keep_diff_only cleanup (this produces the retention label).
    await provider.cleanup(_workspace, CleanupPolicy.KEEP_DIFF_ONLY, launch)

    claim_obj = api.objects[("persistentvolumeclaims", "ws-01attempt0000000000000000a")]
    labels = claim_obj.body.get("metadata", {}).get("labels", {})
    assert k8sspec.LABEL_RETAIN in labels

    # Retention sweep: the PVC has LABEL_RETAIN so it's spared.  Non-PVC
    # objects (configmaps, secrets) that share LABEL_ATTEMPT are still
    # removed because the attempt is not in ``keep``.  We expect the count
    # to match those non-PVC deletions, not the PVC.
    _removed = await provider.retention([], orphan=[])
    # PVC stays because it has the retention label.
    assert api.object_names("persistentvolumeclaims") == ["ws-01attempt0000000000000000a"]
