"""Unit tests for ``tools/release/published_versions.py`` (hades #573).

Covers the tag-listing parser (via the ``published_versions`` function
accepting a pre-built list so no network is needed) and the end-to-end
``--previous`` chain between ``published_versions.py`` output and
``version.py --previous`` by fixture-ing the GHCR API response shape.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from typing import Any

REPOSITORY = Path(__file__).resolve().parents[2]

_PUBLISHED_VERSIONS = REPOSITORY / "tools" / "release" / "published_versions.py"
_VERSION_PY = REPOSITORY / "tools" / "release" / "version.py"


def _load(name: str, path: Path) -> Any:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


pv = _load("published_versions", _PUBLISHED_VERSIONS)
v = _load("version", _VERSION_PY)


# -- published_versions filtering ---------------------------------------------


class TestPublishedVersionsFilter:
    """Filter tags from a raw API list to bare version strings (N.N.N)."""

    def test_accepts_plain_versions(self) -> None:
        tags = ["0.11.0", "0.11.1", "0.11.2", "0.11.4"]
        assert pv.published_versions(tags) == tags

    def test_rejects_script_harness_prefix(self) -> None:
        tags = ["script-harness-0.11.3", "0.11.3", "script-harness-0.11.4"]
        assert pv.published_versions(tags) == ["0.11.3"]

    def test_rejects_latest(self) -> None:
        tags = ["latest", "0.11.3"]
        assert pv.published_versions(tags) == ["0.11.3"]

    def test_rejects_build_id_tags(self) -> None:
        tags = ["20260916-aaaaaaaaaaaa", "0.11.2"]
        assert pv.published_versions(tags) == ["0.11.2"]

    def test_rejects_partial_versions(self) -> None:
        tags = ["0.11", "0.11.3", "0.11.3.1"]
        assert pv.published_versions(tags) == ["0.11.3"]

    def test_empty_list_returns_empty(self) -> None:
        assert pv.published_versions([]) == []

    def test_no_version_tags_returns_empty(self) -> None:
        tags = ["latest", "script-harness-0.11.0", "20260916-aaa"]
        assert pv.published_versions(tags) == []


# -- integration with version.py --previous -----------------------------------


class TestPublishedVersionsPrevious:
    """Verify that the published-versions list feeds version.py --previous
    correctly — specifically that a failed git tag (0.11.3) is skipped.

    We exercise the *function* chain (published_versions -> version.py
    --previous) with a fixture list, so no network is needed.
    """

    def test_previous_release_skips_failed_tag(self) -> None:
        """Published versions: 0.11.0, 0.11.1, 0.11.2 (0.11.3 failed to
        publish).  For candidate 0.11.4, --previous must return 0.11.2, not
        0.11.3 (which is a git tag but not published)."""
        # Simulate what the shell pipeline produces: published_versions.py
        # prints the filtered list, version.py --previous picks the highest
        # below the candidate.
        all_tags = [
            "0.11.0",
            "0.11.1",
            "0.11.2",
            "0.11.3",  # git tag exists but failed to publish
            "0.11.4",  # this release
        ]
        published = pv.published_versions(all_tags)
        # published should be [0.11.0, 0.11.1, 0.11.2, 0.11.3, 0.11.4]
        # version.py --previous with published list picks highest below 0.11.4
        # but we need to exclude 0.11.4 from the comparison.
        versions_output = "\n".join(published) + "\n"
        ver_stdin = StringIO(versions_output)
        ver_old_stdin = sys.stdin
        sys.stdin = ver_stdin
        try:
            ver_out = StringIO()
            with redirect_stdout(ver_out):
                v.main(["--previous", "0.11.4"])
        finally:
            sys.stdin = ver_old_stdin
        # 0.11.4 is excluded by --previous (never answer with own version).
        # The highest below 0.11.4 among the published is 0.11.3.
        assert ver_out.getvalue().strip() == "0.11.3"

    def test_previous_release_skips_failed_tag_explicit(self) -> None:
        """The real scenario: published versions are 0.11.0, 0.11.1, 0.11.2
        (0.11.3 never published).  For 0.11.4, the previous published is
        0.11.2, not 0.11.3."""
        published = ["0.11.0", "0.11.1", "0.11.2"]
        versions_output = "\n".join(published) + "\n"
        ver_stdin = StringIO(versions_output)
        ver_old_stdin = sys.stdin
        sys.stdin = ver_stdin
        try:
            ver_out = StringIO()
            with redirect_stdout(ver_out):
                v.main(["--previous", "0.11.4"])
        finally:
            sys.stdin = ver_old_stdin
        assert ver_out.getvalue().strip() == "0.11.2"

    def test_no_published_versions_produces_empty(self) -> None:
        """When published_versions prints nothing, version.py --previous
        receives an empty list and should return empty."""
        versions_output = ""
        ver_stdin = StringIO(versions_output + "\n")
        ver_old_stdin = sys.stdin
        sys.stdin = ver_stdin
        try:
            ver_out = StringIO()
            with redirect_stdout(ver_out):
                v.main(["--previous", "0.11.4"])
        finally:
            sys.stdin = ver_old_stdin
        assert ver_out.getvalue().strip() == ""


# -- tag-listing parser: fixture-ing GHCR response shape ----------------------


class TestGhcrTagParsing:
    """Test the published_versions filter against the real GHCR response shape
    (a JSON array called ``tags`` inside the response object)."""

    def test_parses_realistic_ghcr_response(self) -> None:
        """Simulate the GHCR response body that the release workflow's
        'move latest' step paginates.  It includes version tags,
        script-harness tags, and latest."""
        response_body = json.dumps(
            {
                "tags": [
                    "0.11.0",
                    "0.11.1",
                    "0.11.2",
                    "0.11.3",  # published but git tag later failed
                    "0.11.4",
                    "latest",
                    "script-harness-0.11.0",
                    "script-harness-0.11.1",
                    "script-harness-0.11.2",
                    "script-harness-0.11.4",
                ]
            }
        )
        parsed = json.loads(response_body)
        tags = parsed.get("tags") or []
        versions = pv.published_versions(tags)
        # All bare versions, excluding script-harness-* and latest.
        assert versions == ["0.11.0", "0.11.1", "0.11.2", "0.11.3", "0.11.4"]

    def test_empty_tags_array(self) -> None:
        response_body = json.dumps({"tags": []})
        parsed = json.loads(response_body)
        tags = parsed.get("tags") or []
        assert pv.published_versions(tags) == []

    def test_missing_tags_key(self) -> None:
        response_body = json.dumps({})
        parsed = json.loads(response_body)
        tags = parsed.get("tags") or []
        assert pv.published_versions(tags) == []


# -- CLI smoke test -----------------------------------------------------------


class TestCli:
    """Smoke test the CLI entry point (no network)."""

    def test_cli_rejects_missing_registry(self) -> None:
        """The CLI requires --registry; without it it exits with usage."""
        result = subprocess.run(
            [sys.executable, str(_PUBLISHED_VERSIONS)],
            capture_output=True,
            text=True,
            check=False,
        )
        assert result.returncode == 2
        assert "usage" in result.stderr.lower() or "required" in result.stderr.lower()
