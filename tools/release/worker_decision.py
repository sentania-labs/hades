#!/usr/bin/env python3
"""Whether a release rebuilds the worker image and the script-harness image, or
reuses the previous release's published ones by digest (hades #476, the operator's
design of 2026-10-06, corrected per finding 01M4CG0K1TQH50GM73R9H9CQRB): reuse the
worker image only when `images/manifest.env`'s `WORKER` tag is unchanged from the tag
the previous release published, and reuse the script-harness image only when its own
`SCRIPT_HARNESS` tag is likewise unchanged. `tools/images/images.sh` builds both
images in the same step, so a release rebuilds when either tag changed, never only
one of them: the manifest's current harness is otherwise never built or published,
and kind/e2e consumers of `SCRIPT_HARNESS` would receive an older harness than the
release source declares. Reused, the release copies the previous version's images to
the new version tag with `docker buildx imagetools create` (no new layers pushed) and
`tools/release/release_notes.py --worker-reused-from` names the reused image and
digest instead of implying a fresh build.

    worker_decision.py --current images/manifest.env --previous /tmp/previous.env

Both files are plain `images/manifest.env` text: `--current` is the candidate's working
tree, `--previous` is `git show <previous-tag>:images/manifest.env`, read as data, not
executed. Prints `reuse=`, `previous_worker_tag=`, `harness_reuse=` and
`previous_harness_tag=` to stdout; a manifest missing its `WORKER` or `SCRIPT_HARNESS`
line is an error, never read as "no previous image".
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import dataclass

_WORKER_LINE = re.compile(r"^WORKER=(.+)$", re.MULTILINE)
_HARNESS_LINE = re.compile(r"^SCRIPT_HARNESS=(.+)$", re.MULTILINE)


class ManifestError(Exception):
    pass


def worker_tag(manifest_text: str) -> str:
    """The `WORKER=` value out of an `images/manifest.env` file's text."""
    match = _WORKER_LINE.search(manifest_text)
    if match is None:
        raise ManifestError("carries no WORKER line")
    return match.group(1).strip()


def harness_tag(manifest_text: str) -> str:
    """The `SCRIPT_HARNESS=` value out of an `images/manifest.env` file's text."""
    match = _HARNESS_LINE.search(manifest_text)
    if match is None:
        raise ManifestError("carries no SCRIPT_HARNESS line")
    return match.group(1).strip()


@dataclass(frozen=True, slots=True)
class WorkerDecision:
    current_tag: str
    previous_tag: str
    current_harness_tag: str
    previous_harness_tag: str

    @property
    def reuse(self) -> bool:
        """Rebuild and push the worker image only when its declared tag actually
        changed; an identical tag means the previous release's image is still
        correct."""
        return self.current_tag == self.previous_tag

    @property
    def harness_reuse(self) -> bool:
        """Rebuild and push the script-harness image only when its own tag changed,
        independently of the worker tag (finding 01M4CG0K1TQH50GM73R9H9CQRB): the two
        images are declared, and must be proven, separately even though the same
        build step produces both."""
        return self.current_harness_tag == self.previous_harness_tag


def decide(current_manifest: str, previous_manifest: str) -> WorkerDecision:
    return WorkerDecision(
        current_tag=worker_tag(current_manifest),
        previous_tag=worker_tag(previous_manifest),
        current_harness_tag=harness_tag(current_manifest),
        previous_harness_tag=harness_tag(previous_manifest),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--current", required=True, help="path to the candidate's manifest")
    parser.add_argument("--previous", required=True, help="path to the previous release's manifest")
    args = parser.parse_args(argv)
    try:
        with open(args.current, encoding="utf-8") as fh:
            current_text = fh.read()
    except OSError as exc:
        print(f"worker_decision: cannot read {args.current}: {exc}", file=sys.stderr)
        return 1
    try:
        with open(args.previous, encoding="utf-8") as fh:
            previous_text = fh.read()
    except OSError as exc:
        print(f"worker_decision: cannot read {args.previous}: {exc}", file=sys.stderr)
        return 1
    try:
        decision = decide(current_text, previous_text)
    except ManifestError as exc:
        print(f"worker_decision: {exc}", file=sys.stderr)
        return 1
    print(f"reuse={'true' if decision.reuse else 'false'}")
    print(f"previous_worker_tag={decision.previous_tag}")
    print(f"harness_reuse={'true' if decision.harness_reuse else 'false'}")
    print(f"previous_harness_tag={decision.previous_harness_tag}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
