"""Run the release selector against registry responses, without Docker or a daemon."""

from __future__ import annotations

import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

import pytest

REPOSITORY = Path(__file__).resolve().parents[2]
SCRIPT = REPOSITORY / "tools" / "release" / "worker_check_reference.py"
REGISTRY = "registry.example/crucible-worker"


def run_selection(
    tmp_path: Path,
    latest: str | None,
    current: str = "candidate-inputs",
    *,
    version: str = "0.10.0",
    error: str | None = None,
    candidate_error: str | None = None,
) -> subprocess.CompletedProcess[str]:
    responses = {
        f"{REGISTRY}:{version}": {
            "stdout": json.dumps({"crucible.build_inputs": current}),
            "error": candidate_error,
        },
        f"{REGISTRY}:latest": {
            "stdout": json.dumps({"crucible.build_inputs": latest}),
            "error": error,
        },
    }
    if latest is None and error is None:
        responses[f"{REGISTRY}:latest"]["error"] = f"ERROR: {REGISTRY}:latest: not found"
    fixture = tmp_path / "registry.json"
    fixture.write_text(json.dumps(responses))
    inspector = tmp_path / "inspect_fixture.py"
    inspector.write_text(
        "import json, sys\n"
        "from pathlib import Path\n"
        "assert sys.argv[1:4] == ['buildx', 'imagetools', 'inspect']\n"
        "assert sys.argv[5:] == ['--format', '{{json .Image.Config.Labels}}']\n"
        "responses = json.loads(Path(__file__).with_name('registry.json').read_text())\n"
        "response = responses[sys.argv[4]]\n"
        "if response['error']:\n"
        "    print(response['error'], file=sys.stderr)\n"
        "    sys.exit(1)\n"
        "print(response['stdout'])\n"
    )
    return subprocess.run(
        [sys.executable, str(SCRIPT), "--registry", REGISTRY, "--version", version],
        cwd=tmp_path,
        env={**os.environ, "DOCKER": shlex.join([sys.executable, str(inspector)])},
        check=False,
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize(
    ("latest", "expected"),
    [("old-inputs", "0.10.0"), ("candidate-inputs", "latest"), (None, "0.10.0")],
    ids=["changed-worker-adds-harness", "unchanged-worker", "first-release"],
)
def test_release_worker_reference(tmp_path: Path, latest: str | None, expected: str) -> None:
    result = run_selection(tmp_path, latest)
    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == f"{REGISTRY}:{expected}"


def git(repository: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repository, check=True, capture_output=True)


@pytest.mark.parametrize("latest", ["newer-inputs", "old-inputs"])
def test_delayed_patch_compares_actual_latest_not_ancestor(tmp_path: Path, latest: str) -> None:
    git(tmp_path, "init", "-q")
    git(tmp_path, "config", "user.name", "Release Test")
    git(tmp_path, "config", "user.email", "release-test@example.com")
    images = tmp_path / "images"
    images.mkdir()
    manifest = images / "manifest.env"
    manifest.write_text("WORKER=crucible-worker:old-inputs\n")
    git(tmp_path, "add", ".")
    git(tmp_path, "commit", "-qm", "older release")
    git(tmp_path, "tag", "v0.9.0")
    manifest.write_text("WORKER=crucible-worker:newer-inputs\n")
    git(tmp_path, "commit", "-qam", "higher published release")
    git(tmp_path, "tag", "v0.10.0")
    git(tmp_path, "checkout", "-q", "--detach", "v0.9.0")
    git(tmp_path, "commit", "--allow-empty", "-qm", "delayed patch with unchanged worker")
    git(tmp_path, "tag", "v0.9.1")

    result = run_selection(tmp_path, latest, "old-inputs", version="0.9.1")
    assert result.returncode == 0, result.stderr
    expected = "latest" if latest == "old-inputs" else "0.9.1"
    assert result.stdout.strip() == f"{REGISTRY}:{expected}"


@pytest.mark.parametrize("error", ["unauthorized", "connection timed out", "HTTP 429"])
def test_registry_errors_stop_selection(tmp_path: Path, error: str) -> None:
    result = run_selection(tmp_path, "candidate-inputs", error=error)
    assert result.returncode == 1
    assert error in result.stderr
    assert not result.stdout


@pytest.mark.parametrize("target", ["candidate", "latest"])
def test_missing_build_inputs_stop_selection(tmp_path: Path, target: str) -> None:
    result = run_selection(
        tmp_path,
        "" if target == "latest" else "candidate-inputs",
        "" if target == "candidate" else "candidate-inputs",
    )
    assert result.returncode == 1
    assert "carries no crucible.build_inputs label" in result.stderr
    assert not result.stdout


def test_missing_candidate_is_not_treated_as_first_release(tmp_path: Path) -> None:
    result = run_selection(tmp_path, None, candidate_error=f"ERROR: {REGISTRY}:0.10.0: not found")
    assert result.returncode == 1
    assert "not found" in result.stderr
    assert not result.stdout
