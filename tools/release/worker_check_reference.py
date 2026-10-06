#!/usr/bin/env python3
"""Choose the published worker reference a release candidate must resolve.

The release pushes its immutable worker version tag before running the service
image's crane check.  When the worker pin changed since the preceding release,
the check must use that new version tag; `latest` still represents the right
worker when the pin did not change.
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

MANIFEST = Path("images/manifest.env")


def worker_pin(manifest: str) -> str:
    """Return the manifest's one non-empty WORKER value."""
    values = [
        line.removeprefix("WORKER=")
        for line in manifest.splitlines()
        if line.startswith("WORKER=") and line != "WORKER="
    ]
    if len(values) != 1:
        raise ValueError(f"expected one WORKER entry, found {len(values)}")
    return values[0]


def check_reference(registry: str, version: str, current: str, previous: str | None) -> str:
    """Select this release's tag for a changed pin, otherwise published latest."""
    changed = previous is None or worker_pin(current) != worker_pin(previous)
    return f"{registry}:{version if changed else 'latest'}"


def git(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, check=False, text=True)


def previous_release_manifest() -> str | None:
    previous = git("describe", "--tags", "--match", "v[0-9]*.[0-9]*.[0-9]*", "--abbrev=0", "HEAD^")
    if previous.returncode != 0:
        return None
    shown = git("show", f"{previous.stdout.strip()}:{MANIFEST.as_posix()}")
    if shown.returncode != 0:
        raise RuntimeError(shown.stderr.strip() or "could not read the previous worker manifest")
    return shown.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--registry", required=True)
    parser.add_argument("--version", required=True)
    args = parser.parse_args(argv)
    try:
        current = MANIFEST.read_text()
        print(check_reference(args.registry, args.version, current, previous_release_manifest()))
    except (OSError, RuntimeError, ValueError) as error:
        print(f"worker_check_reference: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
