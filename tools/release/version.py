#!/usr/bin/env python3
"""Which of a set of tags is the highest published release version, or (hades #476)
the previous one before a candidate.

    version.py <candidate> [tag ...]
    printf '%s\\n' "$TAGS" | version.py <candidate>
    version.py --previous <candidate> [tag ...]

The release's "move latest only if this is the highest published version" step
(`.github/workflows/release.yml`) calls this rule once, before moving the service
image's `latest` and the worker images' `latest` and `script-harness-latest` together:
`latest` follows the highest published version, never merely the most
recent push, so re-publishing an old fix or re-running an old release job never moves
it backwards.

`--previous` answers a different question for the same release (`tools/release/
worker_decision.py`): which published version is the one immediately before this one,
so the worker image build can be compared against what that release published. It
prints nothing, not `candidate`, when there is no earlier version.

Only a tag that is exactly `N.N.N` (all-numeric components) counts as a version;
anything else, a build-id tag, a harness-prefixed tag such as
`script-harness-1.0.0`, or `latest` itself, is ignored.

`candidate` is always included in the comparison pool for the default mode, so that
answer is never empty even when nothing is published yet; `--previous` never includes
`candidate`, since a release is never its own previous release.
"""

from __future__ import annotations

import re
import sys

VERSION = re.compile(r"(\d+)\.(\d+)\.(\d+)")


def is_version(tag: str) -> bool:
    return VERSION.fullmatch(tag) is not None


def version_key(version: str) -> tuple[int, int, int]:
    match = VERSION.fullmatch(version)
    if not match:
        raise ValueError(f"{version!r} is not a release version")
    first, second, third = match.groups()
    return (int(first), int(second), int(third))


def highest_version(candidate: str, tags: list[str]) -> str:
    """The highest version among `candidate` and every tag in `tags` that is itself a
    bare version; a tag that is not a version never enters the comparison."""
    versions = [tag for tag in tags if is_version(tag)]
    versions.append(candidate)
    return max(versions, key=version_key)


def previous_version(candidate: str, tags: list[str]) -> str | None:
    """The highest version among `tags` that sorts strictly below `candidate`, or
    `None` when none does (hades #476: the release's own previous version, to compare
    its published worker image against). `candidate` itself never counts as its own
    previous version, even when `tags` repeats it (a re-run)."""
    versions = [tag for tag in tags if is_version(tag) and tag != candidate]
    earlier = [tag for tag in versions if version_key(tag) < version_key(candidate)]
    return max(earlier, key=version_key) if earlier else None


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("version: usage: version.py [--previous] <candidate> [tag ...]", file=sys.stderr)
        return 2
    previous = argv and argv[0] == "--previous"
    if previous:
        argv = argv[1:]
    if not argv:
        print("version: usage: version.py [--previous] <candidate> [tag ...]", file=sys.stderr)
        return 2
    candidate, *tags = argv
    if not tags:
        tags = [line.strip() for line in sys.stdin if line.strip()]
    if previous:
        print(previous_version(candidate, tags) or "")
    else:
        print(highest_version(candidate, tags))
    return 0


if __name__ == "__main__":
    sys.exit(main())
