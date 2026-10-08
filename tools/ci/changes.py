#!/usr/bin/env python3
"""The `.github/workflows/ci.yml` entry point for hades #476: classify this push's
changed paths and emit the job-gating outputs the non-core jobs read.

    git diff --name-only "$RANGE" | uv run python3 tools/ci/changes.py >> "$GITHUB_OUTPUT"
    tools/ci/changes.py --paths-file paths.txt >> "$GITHUB_OUTPUT"

`crucible.domain.change_class` is the one definition of the classes; this script only
reads the changed paths and prints them as `key=value` lines, so the workflow and
Hades's own certification (which records the same `class` label) never disagree.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

from crucible.domain.change_class import classify


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument(
        "--paths-file",
        help="a file of changed paths, one per line; reads stdin when omitted",
    )
    args = parser.parse_args(argv)
    if args.paths_file:
        text = Path(args.paths_file).read_text(encoding="utf-8")
    else:
        text = sys.stdin.read()
    paths = [line.strip() for line in text.splitlines() if line.strip()]
    result = classify(paths)
    print(f"class={result.label}")
    print(f"images={'true' if result.run_images else 'false'}")
    print(f"kind={'true' if result.run_kind else 'false'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
