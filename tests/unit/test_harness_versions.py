"""A launch outside the adapter's tested range is refused, never warned about (07, 13)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from crucible.adapters.harness.registry import default_registry
from crucible.application.harnesses import check_image_version as _check
from crucible.application.harnesses import egress_allowlist

REGISTRY = default_registry(test_fixtures=True)
MANIFEST = Path(__file__).parents[2] / "images" / "manifest.env"
# The worker image (C11) is named for the day of its pinned inputs; the e2e image keeps
# its harness and version in the tag.
IMAGE_TAG = re.compile(r"^crucible-worker:(?:\d{8}|script-harness-\d+\.\d+\.\d+)-[0-9a-f]{12}$")


def check_image_version(harness: str, labels: dict[str, str]):  # type: ignore[no-untyped-def]
    return _check(REGISTRY, harness, labels)


def labels(harness: str, version: str) -> dict[str, str]:
    """The labels images/build.sh writes for an image carrying one harness."""
    return {"crucible.harnesses": harness, f"crucible.harness.{harness}.version": version}


def legacy_labels(harness: str, version: str) -> dict[str, str]:
    """The labels a per-harness image built before C11 carries."""
    return {"crucible.harness": harness, "crucible.harness_version": version}


def _manifest() -> dict[str, str]:
    return {
        key: value
        for line in MANIFEST.read_text(encoding="utf-8").splitlines()
        if "=" in line and not line.startswith("#")
        for key, value in [line.split("=", maxsplit=1)]
    }


def _worker_labels() -> dict[str, str]:
    """The labels images/build.sh writes for the worker image, read from the manifest
    so this stays in step with the harness versions it pins, not a copy that goes
    stale on the next pin bump."""
    pairs = dict(pair.split(":", 1) for pair in _manifest()["WORKER_HARNESSES"].split(","))
    return {
        "crucible.harnesses": ",".join(pairs),
        **{f"crucible.harness.{harness}.version": version for harness, version in pairs.items()},
    }


WORKER_LABELS = _worker_labels()


def test_every_manifest_harness_version_is_supported_by_its_adapter() -> None:
    """The declared image pins and the adapters' tested ranges move together (13). One
    worker image carries the four real harnesses (C11), so the check reads the harness
    versions the manifest records for each image, which check-manifest.sh holds equal to
    the Dockerfile pins and build.sh writes as the image's labels."""
    manifest = _manifest()
    tags = {k: v for k, v in manifest.items() if not k.endswith(("_DIGEST", "_HARNESSES"))}
    assert set(tags) == {"WORKER", "SCRIPT_HARNESS"}
    carried: dict[str, str] = {}
    for key, tag in tags.items():
        assert IMAGE_TAG.fullmatch(tag), f"{key} has malformed worker image tag: {tag!r}"
        for pair in manifest[f"{key}_HARNESSES"].split(","):
            harness, version = pair.split(":", 1)
            assert harness not in carried, f"{harness} is carried by two images"
            carried[harness] = version
    assert set(carried) == {adapter.name for adapter in REGISTRY}
    for harness, version in carried.items():
        assert REGISTRY.require(harness).supported_versions.supports(version), (harness, version)


def test_the_worker_image_labels_are_checked_per_harness() -> None:
    """13, C11: the launch checks the version the image pins for the harness launched."""
    for harness, version in (
        pair.split(":", 1) for pair in _manifest()["WORKER_HARNESSES"].split(",")
    ):
        check = check_image_version(harness, WORKER_LABELS)
        assert check.ok, check.detail
        assert check.installed == version
    assert not check_image_version("script-harness", WORKER_LABELS).ok
    older = {**WORKER_LABELS, "crucible.harness.codex.version": "0.152.0"}
    assert not check_image_version("codex", older).ok
    assert check_image_version("claude_code", older).ok


def test_a_harness_listed_without_a_version_label_is_refused() -> None:
    listed = {**WORKER_LABELS}
    del listed["crucible.harness.agy.version"]
    check = check_image_version("agy", listed)
    assert not check.ok and "crucible.harness.agy.version" in check.detail


def test_a_version_label_for_a_harness_not_listed_does_not_declare_it() -> None:
    """The list is the declaration; a stray version label does not add a harness."""
    stray = {**labels("codex", "0.156.0"), "crucible.harness.agy.version": "1.2.8"}
    check = check_image_version("agy", stray)
    assert not check.ok and "declares harness codex" in check.detail


def test_an_image_built_before_c11_is_still_read() -> None:
    assert check_image_version("codex", legacy_labels("codex", "0.156.0")).ok
    assert not check_image_version("agy", legacy_labels("codex", "0.156.0")).ok


def test_a_version_inside_the_range_is_accepted() -> None:
    check = check_image_version("codex", labels("codex", "0.156.0"))
    assert check.ok and check.installed == "0.156.0"
    assert check.supported == ">=0.153.0,<0.157.0"


def test_claude_code_version_that_predates_agents_md_is_refused() -> None:
    check = check_image_version("claude_code", labels("claude_code", "2.1.273"))
    assert not check.ok
    assert check.supported == ">=2.1.277,<2.2.0"


def test_claude_code_agents_md_floor_is_accepted() -> None:
    check = check_image_version("claude_code", labels("claude_code", "2.1.277"))
    assert check.ok


@pytest.mark.parametrize("version", ["0.152.9", "0.157.0", "1.0.0"])
def test_a_version_outside_the_range_is_refused(version: str) -> None:
    check = check_image_version("codex", labels("codex", version))
    assert not check.ok
    assert "outside the tested range" in check.detail


def test_an_image_with_no_version_label_is_refused() -> None:
    check = check_image_version("codex", {"crucible.harnesses": "codex"})
    assert not check.ok and "no crucible.harness.codex.version" in check.detail
    check = check_image_version("codex", {"crucible.harness": "codex"})
    assert not check.ok and "no crucible.harness.codex.version" in check.detail


def test_an_image_for_another_harness_is_refused() -> None:
    check = check_image_version("codex", labels("claude_code", "2.1.273"))
    assert not check.ok and "declares harness" in check.detail


def test_an_unknown_harness_is_refused() -> None:
    check = check_image_version("nonesuch", labels("nonesuch", "1.0.0"))
    assert not check.ok and "no adapter declares" in check.detail


def test_an_unparsable_version_is_refused() -> None:
    assert not check_image_version("codex", labels("codex", "latest")).ok


def test_the_script_harness_is_declared_for_the_e2e_tier() -> None:
    adapter = REGISTRY.require("script-harness")
    assert adapter.supported_versions.supports("1.0.0")
    # 18: no model, so no endpoint it must reach.
    assert adapter.capabilities().endpoints == ()


def test_the_allowlist_is_the_union_of_policy_and_adapter_endpoints() -> None:
    """13: the worker allowlist is the policy's list plus the adapter's endpoints (S6)."""
    hosts = egress_allowlist(REGISTRY, "claude_code", ["github.com", "pypi.org"], [])
    assert hosts == ("api.anthropic.com", "github.com", "pypi.org")
    # A local model endpoint the routing policy names is added for that attempt (05b).
    hosts = egress_allowlist(REGISTRY, "codex", ["github.com"], ["spark.example.internal"])
    assert "spark.example.internal" in hosts and "api.openai.com" in hosts


def test_an_image_with_no_harness_label_is_refused_before_launch() -> None:
    """13 (review C1): an image that does not say which harness it carries never runs
    with any harness's credential, whatever its version label says."""
    check = check_image_version("codex", {"crucible.harness_version": "0.156.0"})
    assert not check.ok and "no crucible.harnesses" in check.detail
    check = check_image_version(
        "codex", {"crucible.harness": "", "crucible.harness_version": "0.156.0"}
    )
    assert not check.ok and "no crucible.harnesses" in check.detail
    check = check_image_version("codex", {"crucible.harness.codex.version": "0.156.0"})
    assert not check.ok and "no crucible.harnesses" in check.detail
    assert not check_image_version("codex", labels("claude_code", "0.156.0")).ok
    assert check_image_version("codex", labels("codex", "0.156.0")).ok


def test_qwen_code_version_is_pinned_and_gated() -> None:
    assert check_image_version("qwen_code", WORKER_LABELS).ok
    assert WORKER_LABELS["crucible.harness.qwen_code.version"] == "0.25.0"
    for version in ("0.24.0", "0.26.0"):
        assert not check_image_version("qwen_code", labels("qwen_code", version)).ok
