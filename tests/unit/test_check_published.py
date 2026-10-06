"""Published image checks distinguish branch CI from strict release validation."""

from __future__ import annotations

import pytest

from crucible.adapters.execution.k8sregistry import CraneRegistryClient
from crucible.ports.execution import ImageInfo
from tools.registry import check_published

REFERENCE = "ghcr.io/sentania-labs/crucible-worker:latest"
DIGEST = "sha256:" + "a" * 64


def published(monkeypatch: pytest.MonkeyPatch, labels: dict[str, str]) -> None:
    info = ImageInfo.from_labels(REFERENCE.removesuffix(":latest") + "@" + DIGEST, DIGEST, labels)
    monkeypatch.setattr(CraneRegistryClient, "resolve", lambda self, reference: info)


def test_new_registry_harness_passes_ci_but_fails_release(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    harnesses = check_published.worker_image_harnesses()
    assert "qwen_code" in harnesses
    released = [name for name in harnesses if name != "qwen_code"]
    published(
        monkeypatch,
        {
            "crucible.harnesses": ",".join(released),
            **{f"crucible.harness.{name}.version": "1.0.0" for name in released},
        },
    )
    assert check_published.main(["--reference", REFERENCE, "--expect-image-harnesses"]) == 0
    assert capsys.readouterr().err == ""
    assert check_published.main(["--reference", REFERENCE]) == 1
    assert "no version label for the qwen_code harness" in capsys.readouterr().err
    assert check_published.main(["--reference", REFERENCE, "--expect-harness", "qwen_code"]) == 1


@pytest.mark.parametrize(
    "labels, message",
    [
        ({}, "no crucible.harnesses label"),
        ({"crucible.harnesses": "hermes"}, "no version label for the hermes harness"),
    ],
)
def test_ci_still_requires_declared_harness_versions(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    labels: dict[str, str],
    message: str,
) -> None:
    published(monkeypatch, labels)
    assert check_published.main(["--reference", REFERENCE, "--expect-image-harnesses"]) == 1
    assert message in capsys.readouterr().err


def test_expected_harness_modes_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit) as exc:
        check_published.main(
            ["--reference", REFERENCE, "--expect-image-harnesses", "--expect-harness", "hermes"]
        )
    assert exc.value.code == 2
