"""Hades #140: retention for `ci-*` proof tags on GHCR.

AC1: the selection script picks only versions whose tags are all `ci-*` and older
than N days, and never one tagged with a release, `latest`, or referenced from main.
AC2: dry-run prints the versions it would delete and deletes nothing.
"""

from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from tools.registry import ci_tag_retention as retention

NOW = datetime(2026, 10, 8, tzinfo=UTC)
OLD = NOW - timedelta(days=30)
RECENT = NOW - timedelta(days=1)
PROTECTED_DIGEST = "sha256:" + "a" * 64
OTHER_DIGEST = "sha256:" + "b" * 64
RELEASE_DIGEST = "sha256:" + "c" * 64


def _version(
    version_id: int, tags: tuple[str, ...], created_at: datetime, digest: str = OTHER_DIGEST
) -> retention.PackageVersion:
    return retention.PackageVersion(id=version_id, digest=digest, tags=tags, created_at=created_at)


class FakePackagesClient:
    """A `PackagesPort` the tests drive directly, with no network."""

    def __init__(
        self, versions: list[retention.PackageVersion], bodies: list[str] | None = None
    ) -> None:
        self._versions = versions
        self._bodies = bodies or []
        self.deleted: list[int] = []

    def list_versions(self) -> list[retention.PackageVersion]:
        return list(self._versions)

    def delete_version(self, version_id: int) -> None:
        self.deleted.append(version_id)

    def release_bodies(self) -> list[str]:
        return list(self._bodies)


# ----- AC1: selection excludes everything but an old, ci-*-only, unreferenced version


def test_selects_an_old_ci_only_version() -> None:
    versions = [_version(1, ("ci-20260101-abcdef",), OLD)]
    selected = retention.select_for_deletion(
        versions, now=NOW, max_age_days=14, protected_digests=set()
    )
    assert [v.id for v in selected] == [1]


def test_excludes_a_version_carrying_a_release_tag() -> None:
    """A version with a release tag alongside a ci-* tag is never selected, even old."""
    versions = [_version(1, ("ci-20260101-abcdef", "v1.4.0"), OLD)]
    selected = retention.select_for_deletion(
        versions, now=NOW, max_age_days=14, protected_digests=set()
    )
    assert selected == []


def test_excludes_a_version_carrying_latest() -> None:
    versions = [_version(1, ("ci-20260101-abcdef", "latest"), OLD)]
    selected = retention.select_for_deletion(
        versions, now=NOW, max_age_days=14, protected_digests=set()
    )
    assert selected == []


def test_excludes_an_untagged_version() -> None:
    """Out of scope: #111 only ever skipped the ci-* prefix, nothing broader."""
    versions = [_version(1, (), OLD)]
    selected = retention.select_for_deletion(
        versions, now=NOW, max_age_days=14, protected_digests=set()
    )
    assert selected == []


def test_excludes_a_ci_only_version_younger_than_the_cutoff() -> None:
    versions = [_version(1, ("ci-20260101-abcdef",), RECENT)]
    selected = retention.select_for_deletion(
        versions, now=NOW, max_age_days=14, protected_digests=set()
    )
    assert selected == []


def test_excludes_a_digest_images_manifest_env_on_main_references() -> None:
    versions = [_version(1, ("ci-20260101-abcdef",), OLD, digest=PROTECTED_DIGEST)]
    manifest_text = f"WORKER_DIGEST={PROTECTED_DIGEST}\n"
    protected = retention.parse_manifest_digests(manifest_text)
    selected = retention.select_for_deletion(
        versions, now=NOW, max_age_days=14, protected_digests=protected
    )
    assert selected == []


def test_excludes_a_digest_a_release_body_references() -> None:
    versions = [_version(1, ("ci-20260101-abcdef",), OLD, digest=RELEASE_DIGEST)]
    body = f"crucible-worker:1.4.0@{RELEASE_DIGEST}"
    protected = retention.parse_release_digests([body])
    selected = retention.select_for_deletion(
        versions, now=NOW, max_age_days=14, protected_digests=protected
    )
    assert selected == []


def test_a_release_tagged_version_is_never_selected_even_when_old_and_unreferenced() -> None:
    """Belt and suspenders on AC1's "never a release tag" clause: even with no manifest
    or release-body protection at all, a release-tagged version is excluded by its tags
    alone."""
    versions = [_version(1, ("v2.0.0",), OLD)]
    selected = retention.select_for_deletion(
        versions, now=NOW, max_age_days=14, protected_digests=set()
    )
    assert selected == []


def test_selection_is_per_version_not_all_or_nothing() -> None:
    old_ci_only = _version(1, ("ci-old",), OLD, digest=OTHER_DIGEST)
    old_release = _version(2, ("v1.0.0",), OLD, digest=PROTECTED_DIGEST)
    recent_ci_only = _version(3, ("ci-new",), RECENT, digest="sha256:" + "d" * 64)
    selected = retention.select_for_deletion(
        [old_ci_only, old_release, recent_ci_only],
        now=NOW,
        max_age_days=14,
        protected_digests={PROTECTED_DIGEST},
    )
    assert [v.id for v in selected] == [1]


def test_package_version_from_payload_reads_the_packages_api_shape() -> None:
    payload = {
        "id": 42,
        "name": OTHER_DIGEST,
        "created_at": "2026-09-01T12:00:00Z",
        "metadata": {"package_type": "container", "container": {"tags": ["ci-abc123"]}},
    }
    version = retention.package_version_from_payload(payload)
    assert version == retention.PackageVersion(
        id=42,
        digest=OTHER_DIGEST,
        tags=("ci-abc123",),
        created_at=datetime(2026, 9, 1, 12, 0, 0, tzinfo=UTC),
    )


def test_package_version_from_payload_tolerates_no_tags() -> None:
    payload = {
        "id": 7,
        "name": OTHER_DIGEST,
        "created_at": "2026-09-01T12:00:00Z",
        "metadata": {"package_type": "container", "container": {"tags": []}},
    }
    assert retention.package_version_from_payload(payload).tags == ()


# ----- AC2: dry-run prints and deletes nothing; execute deletes exactly what was printed


def test_dry_run_prints_what_it_would_delete_and_deletes_nothing() -> None:
    client = FakePackagesClient([_version(1, ("ci-old",), OLD)])
    out = io.StringIO()
    selected = retention.run(
        client, manifest_text="", max_age_days=14, now=NOW, execute=False, out=out
    )
    assert [v.id for v in selected] == [1]
    assert client.deleted == []
    printed = out.getvalue()
    assert "would delete" in printed
    assert OTHER_DIGEST in printed
    assert "dry run" in printed


def test_dry_run_prints_nothing_to_delete_when_nothing_is_selected() -> None:
    client = FakePackagesClient([_version(1, ("latest",), OLD)])
    out = io.StringIO()
    selected = retention.run(
        client, manifest_text="", max_age_days=14, now=NOW, execute=False, out=out
    )
    assert selected == []
    assert client.deleted == []
    assert "0 version(s) would be deleted, 0 deleted" in out.getvalue()


def test_execute_deletes_exactly_the_selected_versions() -> None:
    client = FakePackagesClient(
        [
            _version(1, ("ci-old",), OLD, digest=OTHER_DIGEST),
            _version(2, ("latest",), OLD, digest=PROTECTED_DIGEST),
        ]
    )
    out = io.StringIO()
    selected = retention.run(
        client, manifest_text="", max_age_days=14, now=NOW, execute=True, out=out
    )
    assert [v.id for v in selected] == [1]
    assert client.deleted == [1]
    assert "deleting" in out.getvalue()


def test_run_consults_both_manifest_and_release_bodies_for_protection() -> None:
    client = FakePackagesClient(
        [
            _version(1, ("ci-a",), OLD, digest=PROTECTED_DIGEST),
            _version(2, ("ci-b",), OLD, digest=RELEASE_DIGEST),
            _version(3, ("ci-c",), OLD, digest=OTHER_DIGEST),
        ],
        bodies=[f"crucible-worker:1.0.0@{RELEASE_DIGEST}"],
    )
    out = io.StringIO()
    selected = retention.run(
        client,
        manifest_text=f"WORKER_DIGEST={PROTECTED_DIGEST}\n",
        max_age_days=14,
        now=NOW,
        execute=False,
        out=out,
    )
    assert [v.id for v in selected] == [3]


# ----- the CLI wires GITHUB_TOKEN and the manifest file before anything else runs


def test_main_refuses_without_a_github_token(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    assert retention.main([]) == 1
    assert "GITHUB_TOKEN" in capsys.readouterr().err


def test_main_refuses_when_the_manifest_file_is_missing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.setenv("GITHUB_TOKEN", "ghp_test")
    missing = str(tmp_path / "does-not-exist.env")
    assert retention.main(["--manifest-env", missing]) == 1
    assert missing in capsys.readouterr().err
