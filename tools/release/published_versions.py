#!/usr/bin/env python3
"""List published versions for a package on GHCR (hades #573).

Queries the GHCR registry API for all tags of a given package and prints the
bare version components (N.N.N) that appear in those tags, one per line.
Only versions for which BOTH the bare tag and the corresponding
script-harness-<version> tag are published are emitted (hades #570: a release
may push the bare version but fail before pushing script-harness-<version>;
that half-published version must not be selected as the previous release).

    published_versions.py --registry <REGISTRY_URL> [--package <PACKAGE>]

Defaults to ghcr.io/sentania-labs/crucible-worker.  Anonymous access is used:
no credential is required for public packages, and the release workflow uses
this step only for public worker image tags.  GHCR still requires an
anonymous bearer token before returning a tag list; the script performs the
token exchange (request a repo-scoped token without credentials, then use it
as `Authorization: Bearer <token>`) before requesting the tags.

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
import urllib.parse
import urllib.request

VERSION = re.compile(r"^(\d+)\.(\d+)\.(\d+)$")


def _anonymous_token(registry_domain: str, package: str) -> str:
    """Obtain an anonymous bearer token for `package` on `registry_domain`.

    GHCR's Registry v2 endpoints require a bearer token even for public
    repositories.  Request a repo-scoped token without credentials, then
    return the ``token`` value so the caller can include it as an
    ``Authorization: Bearer`` header.
    """
    # Extract org/repo from the package path.
    #   sentania-labs/crucible-worker -> org=sentania-labs, repo=crucible-worker
    parts = package.split("/")
    org = parts[0] if parts else ""
    repo = parts[-1] if parts else ""
    scope = f"repository:{org}/{repo}:pull"

    token_url = f"https://{registry_domain}/token?scope={scope}"
    req = urllib.request.Request(token_url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())
    return str(data["token"])


def fetch_tags(registry: str, package: str) -> list[str]:
    """Return every tag name for `package` on `registry` by paging through the
    registry API.  Uses anonymous access (no credential); GHCR still requires
    an anonymous bearer token, which is obtained before the tag list request.
    """
    # Determine the base URL from the registry
    # ghcr.io/sentania-labs/crucible-worker ->
    #   https://ghcr.io/v2/sentania-labs/crucible-worker/tags/list
    domain = registry
    if domain.startswith("https://"):
        domain = domain[len("https://") :]
    elif domain.startswith("http://"):
        domain = domain[len("http://") :]

    # Obtain an anonymous bearer token (needed for all GHCR API calls).
    token = _anonymous_token(domain, package)

    url = f"https://{domain}/v2/{package}/tags/list?n=100"
    tags: list[str] = []

    while url:
        req = urllib.request.Request(
            url, headers={"Accept": "application/json", "Authorization": f"Bearer {token}"}
        )
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
    """Filter `tags` to bare version strings (N.N.N) for which BOTH the bare
    version tag and the corresponding `script-harness-<version>` tag exist.

    A release that pushes `<version>` but fails before pushing
    `script-harness-<version>` is not a full publish (hades #570); selecting
    it as the previous release would cause the reuse path to fail again when
    it tries to copy the missing harness tag.

    A bare version has three decimal components. Tags like `latest` are
    excluded. Even when no harness tags exist, a bare tag alone is incomplete.
    """
    available = set(tags)
    return [
        tag
        for tag in tags
        if VERSION.fullmatch(tag) is not None and f"script-harness-{tag}" in available
    ]


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

    # Parse the optional scheme before separating the host from the package.
    registry_url = args.registry
    if "://" not in registry_url:
        registry_url = f"https://{registry_url}"
    parsed = urllib.parse.urlsplit(registry_url)
    full_package = parsed.path.strip("/") or args.package

    tags = fetch_tags(parsed.netloc, full_package)
    versions = published_versions(tags)

    for ver in versions:
        print(ver)

    return 0


if __name__ == "__main__":
    sys.exit(main())
