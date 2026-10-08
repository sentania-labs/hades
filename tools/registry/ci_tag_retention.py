"""Delete `ci-*` proof-tag versions from the GHCR worker package (hades#140).

Image listing stopped resolving `ci-*` proof tags (hades#111), but nothing prunes
them, so they accumulate in the `crucible-worker` package's storage forever. This is
the retention step 111 asked the operator's go for; the operator's go was given by
Scott's delegation of 2026-10-07 5:22 PM ("You know the vision- choose answers that
align to the vision"), recorded as Decision: Foundry, N=14 days
(docs/spec/24-release.md).

Selected for deletion: a package version whose tags are all `ci-*` (and there is at
least one) and whose `created_at` is older than `--max-age-days`.

Never selected:

- a version carrying any tag that is not `ci-*` (a release version or `latest`);
- a version with no tags at all (out of this script's scope: #111 only ever skipped
  tags with the `ci-` prefix, nothing broader, and this script mirrors that);
- a version whose digest is named by a `*_DIGEST` line of `images/manifest.env` on
  `main`, or appears in a GitHub release body for this repository -- a release pins
  and republishes images by digest (24), so a version a release still points at is not
  CI's to delete even if every tag it happens to carry starts with `ci-`.

`--dry-run` (the default; deletion needs `--execute`) prints each version that would
be deleted and deletes nothing, so the selection logic is exercised the same way in a
test, in CI and by hand against the real package.

    GITHUB_TOKEN=... python tools/registry/ci_tag_retention.py \\
      --org sentania-labs --package crucible-worker --repo sentania-labs/hades
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Protocol, TextIO

import httpx

API_ROOT = "https://api.github.com"
DEFAULT_ORG = "sentania-labs"
DEFAULT_PACKAGE = "crucible-worker"
DEFAULT_REPO = "sentania-labs/hades"
DEFAULT_MANIFEST_ENV = "images/manifest.env"
DEFAULT_MAX_AGE_DAYS = 14
CI_TAG_PREFIX = "ci-"
DIGEST_RE = re.compile(r"sha256:[0-9a-f]{64}")
PER_PAGE = 100


@dataclass(frozen=True, slots=True)
class PackageVersion:
    """One version of a GHCR container package, as the Packages API describes it."""

    id: int
    digest: str
    tags: tuple[str, ...]
    created_at: datetime


def package_version_from_payload(payload: dict[str, object]) -> PackageVersion:
    metadata = payload.get("metadata")
    container = metadata.get("container") if isinstance(metadata, dict) else None
    raw_tags = container.get("tags") if isinstance(container, dict) else None
    tags = tuple(str(t) for t in raw_tags) if isinstance(raw_tags, list) else ()
    created = str(payload["created_at"]).replace("Z", "+00:00")
    raw_id = payload["id"]
    assert isinstance(raw_id, int)
    return PackageVersion(
        id=raw_id,
        digest=str(payload["name"]),
        tags=tags,
        created_at=datetime.fromisoformat(created),
    )


def is_ci_proof_only(tags: Sequence[str]) -> bool:
    """Whether every tag on a version is a `ci-*` proof tag, and there is at least one."""
    return bool(tags) and all(tag.startswith(CI_TAG_PREFIX) for tag in tags)


def parse_manifest_digests(text: str) -> set[str]:
    """The digests named by the `*_DIGEST=sha256:...` lines of `images/manifest.env`."""
    return {
        match.group(0)
        for line in text.splitlines()
        if "_DIGEST=" in line
        for match in DIGEST_RE.finditer(line)
    }


def parse_release_digests(bodies: Iterable[str]) -> set[str]:
    """Digests a release body records (24: `name:<version>@<digest>`)."""
    return {match.group(0) for body in bodies for match in DIGEST_RE.finditer(body)}


def select_for_deletion(
    versions: Iterable[PackageVersion],
    *,
    now: datetime,
    max_age_days: int,
    protected_digests: set[str],
) -> list[PackageVersion]:
    """Only `ci-*`-only versions, older than `max_age_days`, never a protected digest."""
    cutoff = now - timedelta(days=max_age_days)
    return [
        version
        for version in versions
        if is_ci_proof_only(version.tags)
        and version.created_at < cutoff
        and version.digest not in protected_digests
    ]


class PackagesPort(Protocol):
    """What the retention script needs from GHCR: list, delete, and the release bodies
    that pin digests (24). A fake implementing this is what the unit tests drive."""

    def list_versions(self) -> list[PackageVersion]: ...

    def delete_version(self, version_id: int) -> None: ...

    def release_bodies(self) -> list[str]: ...


class GitHubPackagesClient:
    """`PackagesPort` over the real GitHub Packages and Releases APIs."""

    def __init__(self, token: str, *, org: str, package: str, repo: str, timeout: float = 30.0):
        self._org = org
        self._package = package
        self._repo = repo
        self._client = httpx.Client(
            base_url=API_ROOT,
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": "2022-11-28",
            },
            timeout=timeout,
        )

    def list_versions(self) -> list[PackageVersion]:
        out: list[PackageVersion] = []
        for row in self._paginate(f"/orgs/{self._org}/packages/container/{self._package}/versions"):
            out.append(package_version_from_payload(row))
        return out

    def delete_version(self, version_id: int) -> None:
        response = self._client.delete(
            f"/orgs/{self._org}/packages/container/{self._package}/versions/{version_id}"
        )
        response.raise_for_status()

    def release_bodies(self) -> list[str]:
        return [
            str(row.get("body") or "") for row in self._paginate(f"/repos/{self._repo}/releases")
        ]

    def _paginate(self, path: str) -> list[dict[str, object]]:
        out: list[dict[str, object]] = []
        page = 1
        while True:
            response = self._client.get(path, params={"per_page": PER_PAGE, "page": page})
            response.raise_for_status()
            rows = response.json()
            if not isinstance(rows, list) or not rows:
                break
            out.extend(row for row in rows if isinstance(row, dict))
            if len(rows) < PER_PAGE:
                break
            page += 1
        return out

    def close(self) -> None:
        self._client.close()


def run(
    client: PackagesPort,
    *,
    manifest_text: str,
    max_age_days: int,
    now: datetime,
    execute: bool,
    out: TextIO,
) -> list[PackageVersion]:
    """List, select, print, and (only with `execute`) delete. Returns what was selected,
    so a caller or a test can assert on it without parsing printed output."""
    protected = parse_manifest_digests(manifest_text) | parse_release_digests(
        client.release_bodies()
    )
    selected = select_for_deletion(
        client.list_versions(), now=now, max_age_days=max_age_days, protected_digests=protected
    )
    verb = "deleting" if execute else "would delete"
    for version in selected:
        print(
            f"{verb} {version.digest} id={version.id} tags={','.join(version.tags)} "
            f"created_at={version.created_at.isoformat()}",
            file=out,
        )
    if execute:
        for version in selected:
            client.delete_version(version.id)
    else:
        print(
            f"ci_tag_retention: dry run, {len(selected)} version(s) would be deleted, 0 deleted",
            file=out,
        )
    return selected


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--org", default=DEFAULT_ORG)
    parser.add_argument("--package", default=DEFAULT_PACKAGE)
    parser.add_argument("--repo", default=DEFAULT_REPO)
    parser.add_argument("--manifest-env", default=DEFAULT_MANIFEST_ENV)
    parser.add_argument("--max-age-days", type=int, default=DEFAULT_MAX_AGE_DAYS)
    parser.add_argument(
        "--execute",
        action="store_true",
        help="delete the selected versions; without it, only print what would be deleted",
    )
    args = parser.parse_args(argv)

    token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        print("ci_tag_retention: GITHUB_TOKEN is not set", file=sys.stderr)
        return 1
    try:
        with open(args.manifest_env, encoding="utf-8") as handle:
            manifest_text = handle.read()
    except OSError as exc:
        print(f"ci_tag_retention: {args.manifest_env}: {exc}", file=sys.stderr)
        return 1

    client = GitHubPackagesClient(token, org=args.org, package=args.package, repo=args.repo)
    try:
        run(
            client,
            manifest_text=manifest_text,
            max_age_days=args.max_age_days,
            now=datetime.now(UTC),
            execute=args.execute,
            out=sys.stdout,
        )
    except httpx.HTTPStatusError as exc:
        print(f"ci_tag_retention: {exc}", file=sys.stderr)
        return 1
    finally:
        client.close()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
