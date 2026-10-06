#!/usr/bin/env python3
"""Choose the published worker reference a release candidate must resolve.

The release pushes its immutable worker version tag before this check. Compare
its build inputs with the actual published latest, which belongs to the highest
published version, not necessarily an ancestor of this release. Only identical
inputs allow checking latest instead of this release's version tag.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys


def published_inputs(reference: str, *, allow_absent: bool = False) -> str | None:
    """Read the same build-input label used by the idempotent worker push."""
    result = subprocess.run(
        [
            *shlex.split(os.environ.get("DOCKER", "docker")),
            "buildx",
            "imagetools",
            "inspect",
            reference,
            "--format",
            "{{json .Image.Config.Labels}}",
        ],
        capture_output=True,
        check=False,
        text=True,
    )
    if result.returncode != 0:
        # Match push_worker_images.sh: only buildx's explicit not-found means
        # absent. Authentication and network failures must stop the release.
        last_line = result.stderr.strip().splitlines()[-1:]
        if allow_absent and last_line == [f"ERROR: {reference}: not found"]:
            return None
        raise RuntimeError(result.stderr.strip() or f"could not inspect {reference}")
    labels = json.loads(result.stdout)
    inputs = labels.get("crucible.build_inputs") if isinstance(labels, dict) else None
    if not isinstance(inputs, str) or not inputs.strip():
        raise ValueError(f"{reference} carries no crucible.build_inputs label")
    return inputs


def check_reference(registry: str, version: str) -> str:
    """Use latest only when it carries this release's worker build inputs."""
    candidate = f"{registry}:{version}"
    current = published_inputs(candidate)
    latest = published_inputs(f"{registry}:latest", allow_absent=True)
    return f"{registry}:latest" if latest == current else candidate


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        print(check_reference(args.registry, args.version))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"worker_check_reference: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
