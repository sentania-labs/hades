#!/usr/bin/env python3
"""Render the published image digests as GitHub release notes (FDY-0088, FDY-0096).

    release_notes.py --service-image ghcr.io/sentania-labs/crucible:0.5.3 \
        --worker-repository ghcr.io/sentania-labs/crucible-worker

By the time this runs, the release workflow has pushed the service image and both
worker images with `docker push`, so all three are on the registry. Every digest
printed here is read back from the registry with `docker buildx imagetools inspect`,
never taken from a local build or images/manifest.env: `docker push` re-encodes the
layers, so the local OCI digest is not what anyone pulls.

The worker images' release tags are the service image's own version, `<version>` and
`script-harness-<version>` (13, 24). DOCKER (default `docker`) is the Docker CLI or
the rootless wrapper, already logged in.

Prints markdown to stdout; the release workflow puts it in front of `gh release
create --notes-file` and `--generate-notes` appends the usual changelog after it.
"""

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys


class NotesError(Exception):
    pass


def render(refs: list[tuple[str, str]], latest_note: str) -> str:
    lines = "".join(f"- `{ref}@{digest}`\n" for ref, digest in refs)
    return (
        "## Published image digests\n"
        "\n"
        "Read back from the registry at release time; if this release predates this "
        "section, `docs/deployment.md` gives the equivalent registry command.\n"
        "\n"
        f"{lines}"
        f"{latest_note}"
    )


def registry_digest(ref: str) -> str | None:
    """The manifest digest the registry holds under `ref`, or None when the registry
    says it has no such tag. Any other failure is an error, never "absent"."""
    command = [
        *shlex.split(os.environ.get("DOCKER", "docker")),
        "buildx",
        "imagetools",
        "inspect",
        ref,
        "--format",
        "{{json .Manifest.Digest}}",
    ]
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        stderr = result.stderr.strip()
        if stderr.splitlines()[-1:] == [f"ERROR: {ref}: not found"]:
            return None
        raise NotesError(f"cannot read {ref} from the registry: {stderr}")
    digest = json.loads(result.stdout)
    if not isinstance(digest, str) or not digest.startswith("sha256:"):
        raise NotesError(f"{ref}: the registry answered {result.stdout.strip()!r}")
    return digest


def published(ref: str) -> str:
    digest = registry_digest(ref)
    if digest is None:
        raise NotesError(f"{ref} is not published")
    return digest


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0] if __doc__ else None)
    parser.add_argument("--service-image", required=True, help="repository:version")
    parser.add_argument("--worker-repository", required=True)
    # hades #476: set when the release decided the worker image is unchanged and
    # republished the previous release's image by digest instead of building one.
    parser.add_argument(
        "--worker-reused-from",
        default="",
        help="the previous release's version, when the worker image was reused by digest",
    )
    args = parser.parse_args(argv)
    try:
        _, sep, version = args.service_image.rpartition(":")
        if not sep or "/" in version:
            raise NotesError(f"--service-image {args.service_image!r} names no tag")
        worker = f"{args.worker_repository}:{version}"
        harness = f"{args.worker_repository}:script-harness-{version}"
        refs = [(ref, published(ref)) for ref in (args.service_image, worker, harness)]

        # The "move latest" step moves worker latest only when this version is the
        # highest published; a lower version released after a higher one leaves it
        # pointing elsewhere, which the notes say plainly rather than implying it moved.
        latest = f"{args.worker_repository}:latest"
        latest_note = (
            ""
            if registry_digest(latest) == refs[1][1]
            else f"- `{latest}` was left alone: {version} is not the highest published version.\n"
        )
        # The worker image's own digest is still read back above, from the tag this
        # release just copied it to; this note only says plainly that it is a copy,
        # not a fresh build, naming the release it was reused from.
        reused_note = (
            f"- the worker image is unchanged since `{args.worker_repository}:"
            f"{args.worker_reused_from}`; republished at `{worker}@{refs[1][1]}` by digest, "
            "not rebuilt.\n"
            if args.worker_reused_from
            else ""
        )
        sys.stdout.write(render(refs, latest_note + reused_note))
    except NotesError as exc:
        print(f"release_notes: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
