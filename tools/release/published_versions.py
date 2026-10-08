#!/usr/bin/env python3
"""List published versions for a package on GHCR (hades #573).

Queries the GHCR registry API for all tags of a given package and prints the
bare version components (N.N.N) that appear in those tags, one per line.
Tags that are not bare versions (script-harness-*, latest, etc.) are ignored.

    published_versions.py --registry <REGISTRY_URL> [--package <PACKAGE>]

Defaults to ghcr.io/sentania-labs/crucible-worker.  Anonymous access is used:
no credential is required for public packages, and the release workflow uses
this step only for public worker image tags.

The page-listing loop mirrors the pattern already used in the release workflow
's "move latest" step: paginate with `?n=100` and the Link header's `next`
URL until there is no more pages.  A non-200 / non-404 response is fatal so
that transient network failures are never silently treated as "no versions
published".
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import urllib.error
import urllib.request

VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def fetch_tags(registry: str, package: str) -> list[str]:
    """Return every tag name for `package` on `registry` by paging through the
    registry API.  Uses anonymous access (no auth header)."""
    # Determine the base URL from the registry
    # ghcr.io/sentania-labs/crucible-worker ->
    #   https://ghcr.io/v2/sentania-labs/crucible-worker/tags/list
    domain = registry
    if domain.startswith("https://"):
        domain = domain[len("https://") :]
    elif domain.startswith("http://"):
        domain = domain[len("http://") :]

    url = f"https://{domain}/v2/{package}/tags/list?n=100"
    tags: list[str] = []

    while url:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        try:
            with urllib.request.urlopen(req) as resp:
                body = json.loads(resp.read())
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                # Package does not exist yet — no tags.
                return tags
            # Any other error is fatal: transient network issues must not be
            # treated as "no versions published".
            print(
                f"published_versions: {domain}/{package} tags list "
                f"returned HTTP {exc.code}; refusing to guess",
                file=sys.stderr,
            )
            sys.exit(1)

        tag_list = body.get("tags") or []
        tags.extend(tag_list)

        # Parse Link header for the next page.
        raw_link = resp.headers.get("Link", "")
        next_url = _extract_next_link(raw_link)
        if next_url:
            url = f"https://{domain}{next_url}" if next_url.startswith("/") else next_url
        else:
            url = ""

    return tags


def _extract_next_link(link_header: str) -> str | None:
    """Extract the next-page URL from a GitHub/GHCR Link header."""
    for link_entry in link_header.split(","):
        cleaned = link_entry.strip()
        if ' rel="next"' in cleaned:
            match = re.match(r"<([^>]+)>", cleaned)
            if match:
                return match.group(1)
    return None


def published_versions(tags: list[str]) -> list[str]:
    """Filter `tags` to bare version strings (N.N.N), preserving input order.

    A bare version has three decimal components with no leading zeros.
    Tags like `script-harness-1.0.0` or `latest` are excluded.
    """
    return [tag for tag in tags if VERSION.fullmatch(tag) is not None]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.strip().splitlines()[0])
    parser.add_argument(
        "--registry",
        required=True,
        help="Registry URL, e.g. ghcr.io/sentania-labs/crucible-worker",
    )
    parser.add_argument(
        "--package",
        default="crucible-worker",
        help="Package name within the registry (default: crucible-worker)",
    )
    args = parser.parse_args(argv)

    # Split registry into domain and package path.
    # "ghcr.io/sentania-labs/crucible-worker" -> domain="ghcr.io", package="crucible-worker"
    parts = args.registry.split("/", 1)
    domain = parts[0]
    registry_pkg = parts[1] if len(parts) > 1 else ""

    # Determine the actual package: if registry already includes the full
    # path, use it as-is; otherwise prepend the package name.
    full_package = registry_pkg or args.package

    tags = fetch_tags(f"https://{domain}", full_package)
    versions = published_versions(tags)

    for ver in versions:
        print(ver)

    return 0


if __name__ == "__main__":
    sys.exit(main())
