"""Resolve a published worker image through the Kubernetes provider's registry adapter.

The proof that the adapter reads the real registry the release publishes to (108): an
anonymous pull of the reference, which must come back with a sha256 digest and a
version label for every harness it is expected to carry. By default that is every
harness this release knows except the script harness, which ships in its own e2e image:
the worker image carries all the others (the operator's decision of 2026-09-22). No
credential is used or needed; the package is public.

CI mode: `make registry-check` passes --expect-image-harnesses with the pinned crane
on PATH. The expected harness list comes from the published image's own manifest
config (the crucible.harnesses label), read at its resolved digest. Each declared
harness must have a version. A branch may add a harness before the next release,
so this mode does not require unpublished harnesses from the branch's registry.
An empty declaration still fails.

Release mode: without that flag, require every production harness in the code
registry. The release runs this inside the service image it has just built, also
proving that image ships crane. Keep this strict default for release verification.
For an explicit expected set, repeat --expect-harness; it cannot be combined with
--expect-image-harnesses.

    python tools/registry/check_published.py \\
      --reference ghcr.io/sentania-labs/crucible-worker:latest --expect-image-harnesses
"""

from __future__ import annotations

import argparse
import json
import re
import sys

from crucible.adapters.execution.k8sregistry import CraneRegistryClient, RegistryError
from crucible.adapters.harness import script
from crucible.adapters.harness.registry import default_registry


def worker_image_harnesses() -> list[str]:
    return [name for name in default_registry().names() if name != script.NAME]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--reference", required=True)
    expected = parser.add_mutually_exclusive_group()
    expected.add_argument("--expect-harness", action="append", default=None)
    expected.add_argument(
        "--expect-image-harnesses",
        action="store_true",
        help="check the published image's declared harnesses instead of the code registry",
    )
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args(argv)

    try:
        info = CraneRegistryClient(timeout=args.timeout).resolve(args.reference)
    except RegistryError as exc:
        print(f"check-published: {args.reference} did not resolve: {exc}", file=sys.stderr)
        return 1
    print(
        json.dumps(
            {"reference": args.reference, "pinned": info.reference, "harnesses": info.harnesses},
            indent=2,
        )
    )
    problems = []
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", info.digest):
        problems.append(f"the digest {info.digest!r} is not a sha256 digest")
    if not info.harnesses:
        problems.append("the image carries no crucible.harnesses label")
    harnesses = (
        list(info.harnesses)
        if args.expect_image_harnesses
        else args.expect_harness or worker_image_harnesses()
    )
    for harness in harnesses:
        if not info.version_of(harness):
            problems.append(f"no version label for the {harness} harness")
    for problem in problems:
        print(f"check-published: {args.reference}: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
