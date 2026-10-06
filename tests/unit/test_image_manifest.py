"""The declared image pins must match the build inputs without building images."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
# One worker image carries every real harness (C11); the e2e image stays separate.
IMAGES = ("script-harness", "worker")


def image_copy(tmp_path: Path) -> Path:
    target = tmp_path / "images"
    shutil.copytree(
        REPOSITORY / "images",
        target,
        ignore=shutil.ignore_patterns("out"),
    )
    return target


def check(images: Path) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [str(images / "check-manifest.sh")],
        text=True,
        capture_output=True,
        check=False,
    )


def drifted(result: subprocess.CompletedProcess[str]) -> set[str]:
    prefix = "image manifest drift: "
    return {
        line.removeprefix(prefix).split()[0]
        for line in result.stderr.splitlines()
        if line.startswith(prefix)
    }


def test_a_matching_manifest_passes_from_a_temporary_image_tree(tmp_path: Path) -> None:
    result = check(image_copy(tmp_path))
    assert result.returncode == 0, result.stderr
    assert result.stdout == "image manifest: 2 image tags match build.sh\n"


def test_a_changed_image_file_names_only_that_image(tmp_path: Path) -> None:
    images = image_copy(tmp_path)
    with (images / "script-harness" / "harness.sh").open("a", encoding="utf-8") as stream:
        stream.write("\n# changed in the temporary test copy\n")

    result = check(images)
    assert result.returncode == 1
    assert drifted(result) == {"script-harness"}
    assert "images/build.sh script-harness" in result.stderr


def test_a_changed_pin_drifts_every_image(tmp_path: Path) -> None:
    images = image_copy(tmp_path)
    with (images / "pins.env").open("a", encoding="utf-8") as stream:
        stream.write("\n# changed in the temporary test copy\n")

    result = check(images)
    assert result.returncode == 1
    assert drifted(result) == set(IMAGES)


def test_a_changed_build_script_drifts_every_image(tmp_path: Path) -> None:
    images = image_copy(tmp_path)
    with (images / "build.sh").open("a", encoding="utf-8") as stream:
        stream.write("\n# changed in the temporary test copy\n")

    result = check(images)
    assert result.returncode == 1
    assert drifted(result) == set(IMAGES)


def test_a_missing_manifest_entry_names_the_image_and_fix(tmp_path: Path) -> None:
    images = image_copy(tmp_path)
    manifest = images / "manifest.env"
    manifest.write_text(
        "".join(
            line
            for line in manifest.read_text(encoding="utf-8").splitlines(keepends=True)
            if not line.startswith("WORKER=")
        ),
        encoding="utf-8",
    )

    result = check(images)
    assert result.returncode == 1
    assert drifted(result) == {"worker"}
    assert "images/build.sh worker" in result.stderr


def test_an_orphaned_manifest_entry_is_rejected(tmp_path: Path) -> None:
    images = image_copy(tmp_path)
    shutil.rmtree(images / "script-harness")

    result = check(images)
    assert result.returncode == 1
    assert "manifest key SCRIPT_HARNESS has no image directory" in result.stderr


def test_the_worker_image_declares_every_real_harness_and_its_pin(tmp_path: Path) -> None:
    """build.sh reads each `ARG HARNESS_<NAME>_VERSION` of the worker Dockerfile; the
    manifest records the same list, which is what the version-range test reads (C11)."""
    manifest = (REPOSITORY / "images" / "manifest.env").read_text(encoding="utf-8")
    carried = dict(
        pair.split(":", 1)
        for line in manifest.splitlines()
        if line.startswith("WORKER_HARNESSES=")
        for pair in line.split("=", 1)[1].split(",")
    )
    dockerfile = (REPOSITORY / "images" / "worker" / "Dockerfile").read_text(encoding="utf-8")
    for name, version in carried.items():
        assert f"ARG HARNESS_{name.upper()}_VERSION={version}\n" in dockerfile
    assert set(carried) == {"agy", "claude_code", "codex", "hermes", "qwen_code"}


def test_a_changed_harness_pin_drifts_only_the_worker_image(tmp_path: Path) -> None:
    images = image_copy(tmp_path)
    dockerfile = images / "worker" / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    dockerfile.write_text(
        text.replace("ARG HARNESS_AGY_VERSION=1.2.8", "ARG HARNESS_AGY_VERSION=1.2.9"),
        encoding="utf-8",
    )

    result = check(images)
    assert result.returncode == 1
    assert drifted(result) == {"worker"}


def test_a_hand_edited_harness_list_is_drift(tmp_path: Path) -> None:
    """The recorded harness versions are checked, not only the tag: a manifest that
    claims a version the Dockerfile does not pin fails."""
    images = image_copy(tmp_path)
    manifest = images / "manifest.env"
    manifest.write_text(
        manifest.read_text(encoding="utf-8").replace("codex:0.156.0", "codex:0.153.4"),
        encoding="utf-8",
    )

    result = check(images)
    assert result.returncode == 1
    assert drifted(result) == {"worker"}
    assert "declares harnesses" in result.stderr


def test_a_harness_pinned_twice_to_different_versions_is_refused(tmp_path: Path) -> None:
    """The Hermes pin is declared in two stages; if they disagree the label would lie
    about one of them, so build.sh refuses rather than picking."""
    images = image_copy(tmp_path)
    dockerfile = images / "worker" / "Dockerfile"
    text = dockerfile.read_text(encoding="utf-8")
    first = text.index("ARG HARNESS_HERMES_VERSION=0.19.0")
    dockerfile.write_text(
        text[:first] + text[first:].replace("0.19.0", "0.19.1", 1), encoding="utf-8"
    )

    result = check(images)
    assert result.returncode != 0
    assert "pins hermes to more than one version" in result.stderr
