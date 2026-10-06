"""The release script selects the published worker matching the release."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "release" / "worker_check_reference.py"
REGISTRY = "registry.example/crucible-worker"


def manifest(worker: str) -> str:
    return f"WORKER={worker}\nWORKER_DIGEST=sha256:ignored\n"


def git(repository: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True)


def run_selection(tmp_path: Path, previous: str | None, current: str) -> str:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Release Test")
    git(tmp_path, "config", "user.email", "release-test@example.com")
    images = tmp_path / "images"
    images.mkdir()
    worker_manifest = images / "manifest.env"
    if previous is not None:
        worker_manifest.write_text(manifest(previous))
        git(tmp_path, "add", "images/manifest.env")
        git(tmp_path, "commit", "-qm", "previous release")
        git(tmp_path, "tag", "v0.9.0")
    worker_manifest.write_text(manifest(current))
    (tmp_path / "release-change").write_text("current\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "current release")
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPT),
            "--registry",
            REGISTRY,
            "--version",
            "0.10.0",
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def test_changed_worker_uses_this_releases_published_version(tmp_path: Path) -> None:
    reference = run_selection(tmp_path, "crucible-worker:old-pin", "crucible-worker:new-pin")
    assert reference == f"{REGISTRY}:0.10.0"


def test_unchanged_worker_uses_published_latest(tmp_path: Path) -> None:
    reference = run_selection(tmp_path, "crucible-worker:same-pin", "crucible-worker:same-pin")
    assert reference == f"{REGISTRY}:latest"


def test_first_release_uses_its_published_version(tmp_path: Path) -> None:
    reference = run_selection(tmp_path, None, "crucible-worker:first-pin")
    assert reference == f"{REGISTRY}:0.10.0"
